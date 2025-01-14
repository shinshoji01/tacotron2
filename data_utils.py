import random
import numpy as np
import torch
import torch.utils.data
import os

import layers
from text import text_to_sequence

import sys
sys.path.append("/work/Git/")
from tacotron2.utils import load_wav_to_torch, load_filepaths_and_text


class TextMelLoader(torch.utils.data.Dataset):
    """
        1) loads audio,text pairs
        2) normalizes text and converts them to sequences of one-hot vectors
        3) computes mel-spectrograms from audio files.
    """
    def __init__(self, audiopaths_and_text, hparams):
        self.audiopaths_and_text = load_filepaths_and_text(audiopaths_and_text)
        self.text_cleaners = hparams.text_cleaners
        self.max_wav_value = hparams.max_wav_value
        self.sampling_rate = hparams.sampling_rate
        self.load_mel_from_disk = hparams.load_mel_from_disk
        self.stft = layers.TacotronSTFT(
            hparams.filter_length, hparams.hop_length, hparams.win_length,
            hparams.n_mel_channels, hparams.sampling_rate, hparams.mel_fmin,
            hparams.mel_fmax)
        random.seed(hparams.seed)
        random.shuffle(self.audiopaths_and_text)
        self.Dataset_dir = hparams.Dataset_dir
        self.Feature_dir = hparams.Feature_dir
        self.mel_mean_std = np.load(hparams.mel_mean_std)
        self.normalize_mel = hparams.normalize_mel
        self.blizzard_normalization = hparams.blizzard_normalization

    def get_mel_text_pair(self, audiopath_and_text):
        # separate filename and text
        audiopath, text = audiopath_and_text[0], audiopath_and_text[1]
        text = self.get_text(text)
        mel = self.get_mel(audiopath)
        ed = self.get_ed(audiopath)
        sp = self.get_sp(audiopath)
        word_dir = self.get_worddir(audiopath)
        return (text, mel, ed, sp, word_dir)

    def get_mel(self, filename):
        filename = self.Dataset_dir + filename
        if not self.load_mel_from_disk:
            audio, sampling_rate = load_wav_to_torch(filename)
            if sampling_rate != self.stft.sampling_rate:
                raise ValueError("{} {} SR doesn't match target {} SR".format(
                    sampling_rate, self.stft.sampling_rate))
            audio_norm = audio / self.max_wav_value
            audio_norm = audio_norm.unsqueeze(0)
            audio_norm = torch.autograd.Variable(audio_norm, requires_grad=False)
            melspec = self.stft.mel_spectrogram(audio_norm)
            melspec = torch.squeeze(melspec, 0)
        else:
            melspec = torch.from_numpy(np.load(filename))
            assert melspec.size(0) == self.stft.n_mel_channels, (
                'Mel dimension mismatch: given {}, expected {}'.format(
                    melspec.size(0), self.stft.n_mel_channels))

        if self.normalize_mel:
            melspec = (melspec-self.mel_mean_std[0])/self.mel_mean_std[1]
        return melspec

    def get_text(self, text):
        text_norm = torch.IntTensor(text_to_sequence(text, self.text_cleaners))
        return text_norm
    
    def get_ed(self, filename):
        if self.blizzard_normalization:
            filename = ".".join(filename.split(".")[:-1]) + "_EI.npy"
        else:
            filename = ".".join(filename.split(".")[:-1]) + "_ED.npy"
        filename = self.Dataset_dir + filename
        ed = torch.from_numpy(np.load(filename))
        return ed
    
    def get_sp(self, filename):
        filename = ".".join(filename.split(".")[:-1]) + "_SP.npy"
        filename = self.Dataset_dir + filename
        ed = torch.from_numpy(np.load(filename))
        return ed
    
    def get_worddir(self, filename):
        return np.load(self.Feature_dir + filename.split("/")[-2] + "/" + ".".join(os.path.basename(filename).split(".")[:-1]) + "_words_phones_dir.npy", allow_pickle=True).item()

    def __getitem__(self, index):
        return self.get_mel_text_pair(self.audiopaths_and_text[index])

    def __len__(self):
        return len(self.audiopaths_and_text)


class TextMelCollate():
    """ Zero-pads model inputs and targets based on number of frames per setep
    """
    def __init__(self, n_frames_per_step):
        self.n_frames_per_step = n_frames_per_step

    def __call__(self, batch):
        """Collate's training batch from normalized text and mel-spectrogram
        PARAMS
        ------
        batch: [text_normalized, mel_normalized]
        """
        # Right zero-pad all one-hot text sequences to max input length
        input_lengths, ids_sorted_decreasing = torch.sort(
            torch.LongTensor([len(x[0]) for x in batch]),
            dim=0, descending=True)
        max_input_len = input_lengths[0]

        text_padded = torch.LongTensor(len(batch), max_input_len)
        text_padded.zero_()
        for i in range(len(ids_sorted_decreasing)):
            text = batch[ids_sorted_decreasing[i]][0]
            text_padded[i, :text.size(0)] = text

        # Right zero-pad mel-spec
        num_mels = batch[0][1].size(0)
        max_target_len = max([x[1].size(1) for x in batch])
        if max_target_len % self.n_frames_per_step != 0:
            max_target_len += self.n_frames_per_step - max_target_len % self.n_frames_per_step
            assert max_target_len % self.n_frames_per_step == 0

        # include mel padded and gate padded
        mel_padded = torch.FloatTensor(len(batch), num_mels, max_target_len)
        mel_padded.zero_()
        gate_padded = torch.FloatTensor(len(batch), max_target_len)
        gate_padded.zero_()
        output_lengths = torch.LongTensor(len(batch))
        for i in range(len(ids_sorted_decreasing)):
            mel = batch[ids_sorted_decreasing[i]][1]
            mel_padded[i, :, :mel.size(1)] = mel
            gate_padded[i, mel.size(1)-1:] = 1
            output_lengths[i] = mel.size(1)
        
        # Emotion Intensity
        ed_padded = torch.FloatTensor(len(batch), 12, max_input_len)
        ed_padded.zero_()
        for i in range(len(ids_sorted_decreasing)):
            ed = batch[ids_sorted_decreasing[i]][2]
            ed_padded[i, :, :ed.size(1)] = ed
            
        # Symbol Position
        sp_padded = torch.FloatTensor(len(batch), 3, max_input_len)
        sp_padded.zero_()
        for i in range(len(ids_sorted_decreasing)):
            sp = batch[ids_sorted_decreasing[i]][3]
            sp_padded[i, :, :sp.size(1)] = sp

        return text_padded, input_lengths, mel_padded, gate_padded, \
            output_lengths, ed_padded, sp_padded
