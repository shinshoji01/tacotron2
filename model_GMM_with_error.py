from math import sqrt
import numpy as np
import torch
from torch.autograd import Variable
from torch import nn
from torch.nn import functional as F
from layers import ConvNorm, LinearNorm

import sys
sys.path.append("/work/Git/")
from tacotron2.utils import to_gpu, get_mask_from_lengths

# import sys
# sys.path.append("/work/Git/Tacotronpytorch/")
from Tacotronpytorch.modelsh.model import Decoder_GMM

class GMMAttention(nn.Module):
    def __init__(self, attention_rnn_dim, embedding_dim, attention_dim,
                 attention_location_n_filters, attention_location_kernel_size):
        super(GMMAttention, self).__init__()
        # self.query_layer = LinearNorm(attention_rnn_dim, attention_dim,
        #                               bias=False, w_init_gain='tanh')
        self.memory_layer = LinearNorm(embedding_dim, attention_dim, bias=False,
                                       w_init_gain='tanh')
        # self.v = LinearNorm(attention_dim, 1, bias=False)
        # self.location_layer = LocationLayer(attention_location_n_filters,
        #                                     attention_location_kernel_size,
        #                                     attention_dim)
        self.score_mask_value = 1e-8
        self.K = 8
        self.gmm_version = "1"
        self.eps = 1e-5
        self.mlp = nn.Sequential(
            nn.Linear(attention_rnn_dim, attention_dim, bias=True),
            nn.Tanh(),
            nn.Linear(attention_dim, 3*self.K))
        self.mu_prev = None
        self.j = None
        self.count = 0
        
    def init_attention(self, processed_memory):
        b, t, c = processed_memory.size()
        self.mu_prev = processed_memory.data.new(b, self.K, 1).zero_()
        j = torch.arange(0, t).to(processed_memory.device)
        self.j = j.view(1, 1, t)  # [1, 1, T]
        

    def get_alignment_energies(self, query, processed_memory,
                               attention_weights_cat):
        """
        PARAMS
        ------
        query: decoder output (batch, n_mel_channels * n_frames_per_step)
        processed_memory: processed encoder outputs (B, T_in, attention_dim)
        attention_weights_cat: cumulative and prev. att weights (B, 2, max_time)

        RETURNS
        -------
        alignment (batch, max_time)
        """
        
        self.query = query
        interm_params = self.mlp(query.unsqueeze(1)).view(query.size(0), -1, self.K)  # [B, 3, K]
        omega_hat, delta_hat, sigma_hat = interm_params.chunk(3, dim=1)  # Tuple
        
        # Each [B, K]
        # print(delta_hat)
        omega_hat = omega_hat.squeeze(1)
        delta_hat = delta_hat.squeeze(1)
        sigma_hat = sigma_hat.squeeze(1)
        # Convert intermediate parameters to final mixture parameters
        # Choose version V0/V1/V2
        # Formula from https://arxiv.org/abs/1910.10288
        if self.gmm_version == '0':
            sigma = (torch.sqrt(torch.exp(-sigma_hat) / 2) + self.eps).unsqueeze(-1)  # [B, K, 1]
            delta = torch.exp(delta_hat).unsqueeze(-1)  # [B, K, 1]
            omega = torch.exp(omega_hat).unsqueeze(-1)  # [B, K, 1]
            Z = 1.0
        elif self.gmm_version == '1':
            sigma = (torch.sqrt(torch.exp(sigma_hat)) + self.eps).unsqueeze(-1)
            delta = torch.exp(delta_hat).unsqueeze(-1)
            omega = F.softmax(omega_hat, dim=-1).unsqueeze(-1)
            Z = torch.sqrt(2 * np.pi * sigma**2)
        elif self.gmm_version == '2':
            sigma = (F.softplus(sigma_hat) + self.eps).unsqueeze(-1)
            delta = (F.softplus(delta_hat)).unsqueeze(-1)
            omega = F.softmax(omega_hat, dim=-1).unsqueeze(-1)
            Z = torch.sqrt(2 * np.pi * sigma**2)
        self.delta = delta
        self.sigma = sigma
        self.omega = omega
        self.Z = Z
        # if self.count>-1:
        #     assert False, "combination should be either 'concatenation' or 'addition'"
        # self.count += 1
        
        mu = self.mu_prev + delta  # [B, K, 1]
        # print((delta==0).sum())  # [B, K ,T]alignment)
        # print(((sigma**2)==0).sum())  # [B, K ,T]alignment)
        # print(-(self.j-mu)**2)
        # print((torch.exp(-(self.j-mu)**2)==0).sum())
        # problem: self.j-mu is too small

        # Get alignment(phi in mathtype)
        alignment = omega / Z * torch.exp(-(self.j - mu)**2 / (sigma**2) / 2)  # [B, K ,T]
        alignment = torch.sum(alignment, 1)  # [B, T]
        # print(alignment)

        # Update mu_prev
        self.mu_prev = mu

        return alignment

    def forward(self, attention_hidden_state, memory, processed_memory,
                attention_weights_cat, mask):
        """
        PARAMS
        ------
        attention_hidden_state: attention rnn last output
        memory: encoder outputs
        processed_memory: processed encoder outputs
        attention_weights_cat: previous and cummulative attention weights
        mask: binary mask for padded data
        """
        alignment = self.get_alignment_energies(
            attention_hidden_state, processed_memory, attention_weights_cat)

        if mask is not None:
            alignment.data.masked_fill_(mask, self.score_mask_value)

        # print("start")
        # print(alignment)
        attention_context = torch.bmm(alignment.unsqueeze(1), memory)
        # print(attention_context)
        attention_context = attention_context.squeeze(1)

        return attention_context, alignment

class LocationLayer(nn.Module):
    def __init__(self, attention_n_filters, attention_kernel_size,
                 attention_dim):
        super(LocationLayer, self).__init__()
        padding = int((attention_kernel_size - 1) / 2)
        self.location_conv = ConvNorm(2, attention_n_filters,
                                      kernel_size=attention_kernel_size,
                                      padding=padding, bias=False, stride=1,
                                      dilation=1)
        self.location_dense = LinearNorm(attention_n_filters, attention_dim,
                                         bias=False, w_init_gain='tanh')

    def forward(self, attention_weights_cat):
        processed_attention = self.location_conv(attention_weights_cat)
        processed_attention = processed_attention.transpose(1, 2)
        processed_attention = self.location_dense(processed_attention)
        return processed_attention
    

class Attention(nn.Module):
    def __init__(self, attention_rnn_dim, embedding_dim, attention_dim,
                 attention_location_n_filters, attention_location_kernel_size):
        super(Attention, self).__init__()
        self.query_layer = LinearNorm(attention_rnn_dim, attention_dim,
                                      bias=False, w_init_gain='tanh')
        self.memory_layer = LinearNorm(embedding_dim, attention_dim, bias=False,
                                       w_init_gain='tanh')
        self.v = LinearNorm(attention_dim, 1, bias=False)
        self.location_layer = LocationLayer(attention_location_n_filters,
                                            attention_location_kernel_size,
                                            attention_dim)
        self.score_mask_value = -float("inf")

    def get_alignment_energies(self, query, processed_memory,
                               attention_weights_cat):
        """
        PARAMS
        ------
        query: decoder output (batch, n_mel_channels * n_frames_per_step)
        processed_memory: processed encoder outputs (B, T_in, attention_dim)
        attention_weights_cat: cumulative and prev. att weights (B, 2, max_time)

        RETURNS
        -------
        alignment (batch, max_time)
        """

        processed_query = self.query_layer(query.unsqueeze(1))
        processed_attention_weights = self.location_layer(attention_weights_cat)
        energies = self.v(torch.tanh(
            processed_query + processed_attention_weights + processed_memory))

        energies = energies.squeeze(-1)
        return energies

    def forward(self, attention_hidden_state, memory, processed_memory,
                attention_weights_cat, mask):
        """
        PARAMS
        ------
        attention_hidden_state: attention rnn last output
        memory: encoder outputs
        processed_memory: processed encoder outputs
        attention_weights_cat: previous and cummulative attention weights
        mask: binary mask for padded data
        """
        alignment = self.get_alignment_energies(
            attention_hidden_state, processed_memory, attention_weights_cat)

        if mask is not None:
            alignment.data.masked_fill_(mask, self.score_mask_value)

        attention_weights = F.softmax(alignment, dim=1)
        attention_context = torch.bmm(attention_weights.unsqueeze(1), memory)
        attention_context = attention_context.squeeze(1)

        return attention_context, attention_weights


class Prenet(nn.Module):
    def __init__(self, in_dim, sizes):
        super(Prenet, self).__init__()
        in_sizes = [in_dim] + sizes[:-1]
        self.layers = nn.ModuleList(
            [LinearNorm(in_size, out_size, bias=False)
             for (in_size, out_size) in zip(in_sizes, sizes)])

    def forward(self, x):
        for linear in self.layers:
            x = F.dropout(F.relu(linear(x)), p=0.5, training=True)
        return x


class Postnet(nn.Module):
    """Postnet
        - Five 1-d convolution with 512 channels and kernel size 5
    """

    def __init__(self, hparams):
        super(Postnet, self).__init__()
        self.convolutions = nn.ModuleList()

        self.convolutions.append(
            nn.Sequential(
                ConvNorm(hparams.n_mel_channels, hparams.postnet_embedding_dim,
                         kernel_size=hparams.postnet_kernel_size, stride=1,
                         padding=int((hparams.postnet_kernel_size - 1) / 2),
                         dilation=1, w_init_gain='tanh'),
                nn.BatchNorm1d(hparams.postnet_embedding_dim))
        )

        for i in range(1, hparams.postnet_n_convolutions - 1):
            self.convolutions.append(
                nn.Sequential(
                    ConvNorm(hparams.postnet_embedding_dim,
                             hparams.postnet_embedding_dim,
                             kernel_size=hparams.postnet_kernel_size, stride=1,
                             padding=int((hparams.postnet_kernel_size - 1) / 2),
                             dilation=1, w_init_gain='tanh'),
                    nn.BatchNorm1d(hparams.postnet_embedding_dim))
            )

        self.convolutions.append(
            nn.Sequential(
                ConvNorm(hparams.postnet_embedding_dim, hparams.n_mel_channels,
                         kernel_size=hparams.postnet_kernel_size, stride=1,
                         padding=int((hparams.postnet_kernel_size - 1) / 2),
                         dilation=1, w_init_gain='linear'),
                nn.BatchNorm1d(hparams.n_mel_channels))
            )

    def forward(self, x):
        for i in range(len(self.convolutions) - 1):
            x = F.dropout(torch.tanh(self.convolutions[i](x)), 0.5, self.training)
        x = F.dropout(self.convolutions[-1](x), 0.5, self.training)

        return x


class Encoder(nn.Module):
    """Encoder module:
        - Three 1-d convolution banks
        - Bidirectional LSTM
    """
    def __init__(self, hparams):
        super(Encoder, self).__init__()

        convolutions = []
        for _ in range(hparams.encoder_n_convolutions):
            conv_layer = nn.Sequential(
                ConvNorm(hparams.encoder_embedding_dim,
                         hparams.encoder_embedding_dim,
                         kernel_size=hparams.encoder_kernel_size, stride=1,
                         padding=int((hparams.encoder_kernel_size - 1) / 2),
                         dilation=1, w_init_gain='relu'),
                nn.BatchNorm1d(hparams.encoder_embedding_dim))
            convolutions.append(conv_layer)
        self.convolutions = nn.ModuleList(convolutions)

        self.lstm = nn.LSTM(hparams.encoder_embedding_dim,
                            int(hparams.encoder_embedding_dim / 2), 1,
                            batch_first=True, bidirectional=True)

    def forward(self, x, input_lengths):
        for conv in self.convolutions:
            x = F.dropout(F.relu(conv(x)), 0.5, self.training)

        x = x.transpose(1, 2)

        # pytorch tensor are not reversible, hence the conversion
        input_lengths = input_lengths.cpu().numpy()
        x = nn.utils.rnn.pack_padded_sequence(
            x, input_lengths, batch_first=True)

        self.lstm.flatten_parameters()
        outputs, _ = self.lstm(x)

        outputs, _ = nn.utils.rnn.pad_packed_sequence(
            outputs, batch_first=True)

        return outputs

    def inference(self, x):
        for conv in self.convolutions:
            x = F.dropout(F.relu(conv(x)), 0.5, self.training)

        x = x.transpose(1, 2)

        self.lstm.flatten_parameters()
        outputs, _ = self.lstm(x)

        return outputs


class Decoder(nn.Module):
    def __init__(self, hparams):
        super(Decoder, self).__init__()
        self.n_mel_channels = hparams.n_mel_channels
        self.n_frames_per_step = hparams.n_frames_per_step
        if hparams.include_ed and hparams.combination=="concatenation":
            self.encoder_embedding_dim = hparams.encoder_embedding_dim + np.array(hparams["phones_words_utterance"]).sum()*4
        else:
            self.encoder_embedding_dim = hparams.encoder_embedding_dim
        self.attention_rnn_dim = hparams.attention_rnn_dim
        self.decoder_rnn_dim = hparams.decoder_rnn_dim
        self.prenet_dim = hparams.prenet_dim
        self.max_decoder_steps = hparams.max_decoder_steps
        self.gate_threshold = hparams.gate_threshold
        self.p_attention_dropout = hparams.p_attention_dropout
        self.p_decoder_dropout = hparams.p_decoder_dropout
        self.attention_type = hparams.attention_type

        self.prenet = Prenet(
            hparams.n_mel_channels * hparams.n_frames_per_step,
            [hparams.prenet_dim, hparams.prenet_dim])

        self.attention_rnn = nn.LSTMCell(
            hparams.prenet_dim + self.encoder_embedding_dim,
            hparams.attention_rnn_dim)

        if self.attention_type=="LST":
            self.attention_layer = Attention(
                hparams.attention_rnn_dim, self.encoder_embedding_dim,
                hparams.attention_dim, hparams.attention_location_n_filters,
                hparams.attention_location_kernel_size)
        elif self.attention_type=="GMM":
            self.attention_layer = GMMAttention(
                hparams.attention_rnn_dim, self.encoder_embedding_dim,
                hparams.attention_dim, hparams.attention_location_n_filters,
                hparams.attention_location_kernel_size)
        else:
            assert False, "attention_type should be either 'LST' or 'GMM'"

        self.decoder_rnn = nn.LSTMCell(
            hparams.attention_rnn_dim + self.encoder_embedding_dim,
            hparams.decoder_rnn_dim, 1)

        self.linear_projection = LinearNorm(
            hparams.decoder_rnn_dim + self.encoder_embedding_dim,
            hparams.n_mel_channels * hparams.n_frames_per_step)

        self.gate_layer = LinearNorm(
            hparams.decoder_rnn_dim + self.encoder_embedding_dim, 1,
            bias=True, w_init_gain='sigmoid')

    def get_go_frame(self, memory):
        """ Gets all zeros frames to use as first decoder input
        PARAMS
        ------
        memory: decoder outputs

        RETURNS
        -------
        decoder_input: all zeros frames
        """
        B = memory.size(0)
        decoder_input = Variable(memory.data.new(
            B, self.n_mel_channels * self.n_frames_per_step).zero_())
        return decoder_input

    def initialize_decoder_states(self, memory, mask):
        """ Initializes attention rnn states, decoder rnn states, attention
        weights, attention cumulative weights, attention context, stores memory
        and stores processed memory
        PARAMS
        ------
        memory: Encoder outputs
        mask: Mask for padded data if training, expects None for inference
        """
        B = memory.size(0)
        MAX_TIME = memory.size(1)

        self.attention_hidden = Variable(memory.data.new(
            B, self.attention_rnn_dim).zero_())
        self.attention_cell = Variable(memory.data.new(
            B, self.attention_rnn_dim).zero_())

        self.decoder_hidden = Variable(memory.data.new(
            B, self.decoder_rnn_dim).zero_())
        self.decoder_cell = Variable(memory.data.new(
            B, self.decoder_rnn_dim).zero_())

        self.attention_weights = Variable(memory.data.new(
            B, MAX_TIME).zero_())
        if self.attention_type=="LST":
            self.attention_weights_cum = Variable(memory.data.new(
                B, MAX_TIME).zero_())
        else:
            self.attention_weights = None
        self.attention_weights_cat = None
        self.attention_context = Variable(memory.data.new(
            B, self.encoder_embedding_dim).zero_())

        self.memory = memory
        # print(memory.shape)
        self.processed_memory = self.attention_layer.memory_layer(memory)
        # print(self.processed_memory.shape)
        self.mask = mask
        if self.attention_type=="GMM":
            self.attention_layer.init_attention(self.processed_memory)

    def parse_decoder_inputs(self, decoder_inputs):
        """ Prepares decoder inputs, i.e. mel outputs
        PARAMS
        ------
        decoder_inputs: inputs used for teacher-forced training, i.e. mel-specs

        RETURNS
        -------
        inputs: processed decoder inputs

        """
        # (B, n_mel_channels, T_out) -> (B, T_out, n_mel_channels)
        decoder_inputs = decoder_inputs.transpose(1, 2)
        decoder_inputs = decoder_inputs.view(
            decoder_inputs.size(0),
            int(decoder_inputs.size(1)/self.n_frames_per_step), -1)
        # (B, T_out, n_mel_channels) -> (T_out, B, n_mel_channels)
        decoder_inputs = decoder_inputs.transpose(0, 1)
        return decoder_inputs

    def parse_decoder_outputs(self, mel_outputs, gate_outputs, alignments):
        """ Prepares decoder outputs for output
        PARAMS
        ------
        mel_outputs:
        gate_outputs: gate output energies
        alignments:

        RETURNS
        -------
        mel_outputs:
        gate_outpust: gate output energies
        alignments:
        """
        # (T_out, B) -> (B, T_out)
        alignments = torch.stack(alignments).transpose(0, 1)
        # (T_out, B) -> (B, T_out)
        gate_outputs = torch.stack(gate_outputs).transpose(0, 1)
        gate_outputs = gate_outputs.contiguous()
        # (T_out, B, n_mel_channels) -> (B, T_out, n_mel_channels)
        mel_outputs = torch.stack(mel_outputs).transpose(0, 1).contiguous()
        # decouple frames per step
        mel_outputs = mel_outputs.view(
            mel_outputs.size(0), -1, self.n_mel_channels)
        # (B, T_out, n_mel_channels) -> (B, n_mel_channels, T_out)
        mel_outputs = mel_outputs.transpose(1, 2)

        return mel_outputs, gate_outputs, alignments

    def decode(self, decoder_input):
        """ Decoder step using stored states, attention and memory
        PARAMS
        ------
        decoder_input: previous mel output

        RETURNS
        -------
        mel_output:
        gate_output: gate output energies
        attention_weights:
        """
        cell_input = torch.cat((decoder_input, self.attention_context), -1)
        self.attention_hidden, self.attention_cell = self.attention_rnn(
            cell_input, (self.attention_hidden, self.attention_cell))
        self.attention_hidden = F.dropout(
            self.attention_hidden, self.p_attention_dropout, self.training)

        if self.attention_type=="LST":
            self.attention_weights_cat = torch.cat(
                (self.attention_weights.unsqueeze(1),
                 self.attention_weights_cum.unsqueeze(1)), dim=1)
        self.attention_context, self.attention_weights = self.attention_layer(
            self.attention_hidden, self.memory, self.processed_memory,
            self.attention_weights_cat, self.mask)

        if self.attention_type=="LST":
            self.attention_weights_cum += self.attention_weights
        decoder_input = torch.cat(
            (self.attention_hidden, self.attention_context), -1)
        self.decoder_hidden, self.decoder_cell = self.decoder_rnn(
            decoder_input, (self.decoder_hidden, self.decoder_cell))
        self.decoder_hidden = F.dropout(
            self.decoder_hidden, self.p_decoder_dropout, self.training)

        decoder_hidden_attention_context = torch.cat(
            (self.decoder_hidden, self.attention_context), dim=1)
        decoder_output = self.linear_projection(
            decoder_hidden_attention_context)

        gate_prediction = self.gate_layer(decoder_hidden_attention_context)
        return decoder_output, gate_prediction, self.attention_weights

    def forward(self, memory, decoder_inputs, memory_lengths):
        """ Decoder forward pass for training
        PARAMS
        ------
        memory: Encoder outputs
        decoder_inputs: Decoder inputs for teacher forcing. i.e. mel-specs
        memory_lengths: Encoder output lengths for attention masking.

        RETURNS
        -------
        mel_outputs: mel outputs from the decoder
        gate_outputs: gate outputs from the decoder
        alignments: sequence of attention weights from the decoder
        """

        decoder_input = self.get_go_frame(memory).unsqueeze(0)
        decoder_inputs = self.parse_decoder_inputs(decoder_inputs)
        decoder_inputs = torch.cat((decoder_input, decoder_inputs), dim=0)
        decoder_inputs = self.prenet(decoder_inputs)
        self.initialize_decoder_states(
            memory, mask=~get_mask_from_lengths(memory_lengths))
        # assert False, "combination should be either 'concatenation' or 'addition'"

        mel_outputs, gate_outputs, alignments = [], [], []
        while len(mel_outputs) < decoder_inputs.size(0) - 1:
            decoder_input = decoder_inputs[len(mel_outputs)]
            # decoder_input = self.prenet(decoder_input)
            mel_output, gate_output, attention_weights = self.decode(
                decoder_input)
            mel_outputs += [mel_output.squeeze(1)]
            gate_outputs += [gate_output.squeeze(1)]
            alignments += [attention_weights]

        mel_outputs, gate_outputs, alignments = self.parse_decoder_outputs(
            mel_outputs, gate_outputs, alignments)

        return mel_outputs, gate_outputs, alignments

    def inference(self, memory):
        """ Decoder inference
        PARAMS
        ------
        memory: Encoder outputs

        RETURNS
        -------
        mel_outputs: mel outputs from the decoder
        gate_outputs: gate outputs from the decoder
        alignments: sequence of attention weights from the decoder
        """
        decoder_input = self.get_go_frame(memory)

        self.initialize_decoder_states(memory, mask=None)

        mel_outputs, gate_outputs, alignments = [], [], []
        while True:
            decoder_input = self.prenet(decoder_input)
            mel_output, gate_output, alignment = self.decode(decoder_input)

            mel_outputs += [mel_output.squeeze(1)]
            gate_outputs += [gate_output]
            alignments += [alignment]

            if torch.sigmoid(gate_output.data) > self.gate_threshold:
                break
            elif len(mel_outputs) == self.max_decoder_steps:
                print("Warning! Reached max decoder steps")
                break

            decoder_input = mel_output

        mel_outputs, gate_outputs, alignments = self.parse_decoder_outputs(
            mel_outputs, gate_outputs, alignments)

        return mel_outputs, gate_outputs, alignments


class Tacotron2(nn.Module):
    def __init__(self, hparams):
        super(Tacotron2, self).__init__()
        self.mask_padding = hparams.mask_padding
        self.fp16_run = hparams.fp16_run
        self.n_mel_channels = hparams.n_mel_channels
        self.n_frames_per_step = hparams.n_frames_per_step
        self.embedding = nn.Embedding(
            hparams.n_symbols, hparams.symbols_embedding_dim)
        std = sqrt(2.0 / (hparams.n_symbols + hparams.symbols_embedding_dim))
        val = sqrt(3.0) * std  # uniform bounds for std
        self.embedding.weight.data.uniform_(-val, val)
        self.encoder = Encoder(hparams)
        self.decoder = Decoder(hparams)
        # if hparams.attention_type=="LST":
        #     self.decoder = Decoder(hparams)
        # elif hparams.attention_type=="GMM":
        #     self.decoder = Decoder_GMM(hparams.n_mel_channels,
        #                                hparams.n_frames_per_step,
        #                                hparams.encoder_embedding_dim,
        #                                [hparams.prenet_dim, hparams.prenet_dim],
        #                                0.5,
        #                                hparams.attention_dim,
        #                                hparams.attention_rnn_dim,
        #                                hparams.p_attention_dropout,
        #                                hparams.decoder_rnn_dim,
        #                                None,
        #                                hparams.p_decoder_dropout,
        #                                hparams.max_decoder_steps,
        #                                hparams.gate_threshold)
        self.postnet = Postnet(hparams)
        self.include_ed = hparams.include_ed
        self.combination = hparams.combination
        self.ed_bool_list = np.array(hparams["phones_words_utterance"]).repeat(4)
        self.attention_type = hparams.attention_type
        self.concatenation_embedding = hparams.concatenation_embedding
        if hparams.include_ed:
            if hparams.combination=="addition":
                self.ed_embedding = LinearNorm(self.ed_bool_list.sum(), hparams.encoder_embedding_dim,
                                               bias=False, w_init_gain='tanh')
            elif hparams.combination=="concatenation":
                if hparams.concatenation_embedding:
                    self.ed_embedding = LinearNorm(self.ed_bool_list.sum(), self.ed_bool_list.sum())
            else:
                assert False, "combination should be either 'concatenation' or 'addition'"

    def parse_batch(self, batch):
        text_padded, input_lengths, mel_padded, gate_padded, \
            output_lengths, ed_padded, sp_padded = batch
        text_padded = to_gpu(text_padded).long()
        input_lengths = to_gpu(input_lengths).long()
        max_len = torch.max(input_lengths.data).item()
        mel_padded = to_gpu(mel_padded).float()
        gate_padded = to_gpu(gate_padded).float()
        output_lengths = to_gpu(output_lengths).long()
        ed_padded = to_gpu(ed_padded).float()
        sp_padded = to_gpu(sp_padded).float()

        return (
            (text_padded, input_lengths, mel_padded, max_len, output_lengths, ed_padded, sp_padded),
            (mel_padded, gate_padded))

    def parse_output(self, outputs, output_lengths=None):
        if self.mask_padding and output_lengths is not None:
            mask = ~get_mask_from_lengths(output_lengths)
            mask = mask.expand(self.n_mel_channels, mask.size(0), mask.size(1))
            mask = mask.permute(1, 0, 2)

            outputs[0].data.masked_fill_(mask, 0.0)
            outputs[1].data.masked_fill_(mask, 0.0)
            outputs[2].data.masked_fill_(mask[:, 0, :], 1e3)  # gate energies

        return outputs

    def forward(self, inputs):
        text_inputs, text_lengths, mels, max_len, output_lengths, ed, sp = inputs
        text_lengths, output_lengths = text_lengths.data, output_lengths.data
        ed = ed[:, self.ed_bool_list, :]

        embedded_inputs = self.embedding(text_inputs).transpose(1, 2)

        encoder_outputs = self.encoder(embedded_inputs, text_lengths)
        if self.include_ed:
            if self.combination=="concatenation":
                if self.concatenation_embedding:
                    encoder_outputs = torch.cat([encoder_outputs, self.ed_embedding(ed.transpose(1, 2))], axis=2)
                else:
                    encoder_outputs = torch.cat([encoder_outputs, ed.transpose(1, 2)], axis=2)
            elif self.combination=="addition":
                encoder_outputs = encoder_outputs + self.ed_embedding(ed.transpose(1, 2))
            else:
                assert False, "combination should be either 'concatenation' or 'addition'"

        mel_outputs, gate_outputs, alignments = self.decoder(
            encoder_outputs, mels, memory_lengths=text_lengths)
        # if self.attention_type=="LST":
        #     mel_outputs, gate_outputs, alignments = self.decoder(
        #         encoder_outputs, mels, memory_lengths=text_lengths)
        # elif self.attention_type=="GMM":
        #     mel_outputs, gate_outputs, alignments = self.decoder(
        #         encoder_outputs, mels.transpose(1, 2), memory_lengths=text_lengths)
        #     mel_outputs = mel_outputs.transpose(1, 2)
        # else:
        #     assert False, "attention_type should be either 'LST' or 'GMM'"

        mel_outputs_postnet = self.postnet(mel_outputs)
        mel_outputs_postnet = mel_outputs + mel_outputs_postnet

        return self.parse_output(
            [mel_outputs, mel_outputs_postnet, gate_outputs, alignments],
            output_lengths)

    def inference(self, inputs):
        inputs, ed = inputs
        ed = ed[:, self.ed_bool_list, :]
        embedded_inputs = self.embedding(inputs).transpose(1, 2)
        encoder_outputs = self.encoder.inference(embedded_inputs)
        if self.include_ed:
            if self.combination=="concatenation":
                if self.concatenation_embedding:
                    encoder_outputs = torch.cat([encoder_outputs, self.ed_embedding(ed.transpose(1, 2))], axis=2)
                else:
                    encoder_outputs = torch.cat([encoder_outputs, ed.transpose(1, 2)], axis=2)
            # encoder_outputs = torch.cat([encoder_outputs, ed.transpose(1, 2)], axis=2)
            elif self.combination=="addition":
                encoder_outputs = encoder_outputs + self.ed_embedding(ed.transpose(1, 2))
            else:
                assert False, "combination should be either 'concatenation' or 'addition'"
        mel_outputs, gate_outputs, alignments = self.decoder.inference(encoder_outputs)
        # if self.attention_type=="LST":
        #     mel_outputs, gate_outputs, alignments = self.decoder.inference(encoder_outputs)
        # elif self.attention_type=="GMM":
        #     mel_outputs, gate_outputs, alignments = self.decoder(
        #         encoder_outputs, None, memory_lengths=None)
        #     mel_outputs = mel_outputs.transpose(1, 2)
        # else:
        #     assert False, "attention_type should be either 'LST' or 'GMM'"

        mel_outputs_postnet = self.postnet(mel_outputs)
        mel_outputs_postnet = mel_outputs + mel_outputs_postnet

        outputs = self.parse_output(
            [mel_outputs, mel_outputs_postnet, gate_outputs, alignments])

        return outputs
