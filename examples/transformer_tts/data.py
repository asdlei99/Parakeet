# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from pathlib import Path
import numpy as np
import pandas as pd
import librosa
import csv

from paddle import fluid
from parakeet import g2p
from parakeet.data.sampler import *
from parakeet.data.datacargo import DataCargo
from parakeet.data.batch import TextIDBatcher, SpecBatcher
from parakeet.data.dataset import DatasetMixin, TransformDataset, CacheDataset, SliceDataset
from parakeet.models.transformer_tts.utils import *


class LJSpeechLoader:
    def __init__(self,
                 config,
                 place,
                 data_path,
                 batch_size,
                 nranks,
                 rank,
                 is_vocoder=False,
                 shuffle=True):

        LJSPEECH_ROOT = Path(data_path)
        metadata = LJSpeechMetaData(LJSPEECH_ROOT)
        transformer = LJSpeech(config)
        dataset = TransformDataset(metadata, transformer)
        dataset = CacheDataset(dataset)

        sampler = DistributedSampler(
            len(dataset), nranks, rank, shuffle=shuffle)

        assert batch_size % nranks == 0
        each_bs = batch_size // nranks
        if is_vocoder:
            dataloader = DataCargo(
                dataset,
                sampler=sampler,
                batch_size=each_bs,
                shuffle=shuffle,
                batch_fn=batch_examples_vocoder,
                drop_last=True)
        else:
            dataloader = DataCargo(
                dataset,
                sampler=sampler,
                batch_size=each_bs,
                shuffle=shuffle,
                batch_fn=batch_examples,
                drop_last=True)
        self.reader = fluid.io.DataLoader.from_generator(
            capacity=32,
            iterable=True,
            use_double_buffer=True,
            return_list=True)
        self.reader.set_batch_generator(dataloader, place)


class LJSpeechMetaData(DatasetMixin):
    def __init__(self, root):
        self.root = Path(root)
        self._wav_dir = self.root.joinpath("wavs")
        csv_path = self.root.joinpath("metadata.csv")
        self._table = pd.read_csv(
            csv_path,
            sep="|",
            header=None,
            quoting=csv.QUOTE_NONE,
            names=["fname", "raw_text", "normalized_text"])

    def get_example(self, i):
        fname, raw_text, normalized_text = self._table.iloc[i]
        fname = str(self._wav_dir.joinpath(fname + ".wav"))
        return fname, raw_text, normalized_text

    def __len__(self):
        return len(self._table)


class LJSpeech(object):
    def __init__(self, config):
        super(LJSpeech, self).__init__()
        self.config = config
        self.sr = config['sr']
        self.n_mels = config['num_mels']
        self.preemphasis = config['preemphasis']
        self.n_fft = config['n_fft']
        self.win_length = config['win_length']
        self.hop_length = config['hop_length']
        self.fmin = config['fmin']
        self.fmax = config['fmax']

    def __call__(self, metadatum):
        """All the code for generating an Example from a metadatum. If you want a 
        different preprocessing pipeline, you can override this method. 
        This method may require several processor, each of which has a lot of options.
        In this case, you'd better pass a composed transform and pass it to the init
        method.
        """
        fname, raw_text, normalized_text = metadatum

        # load
        wav, _ = librosa.load(str(fname))

        spec = librosa.stft(
            y=wav,
            n_fft=self.n_fft,
            win_length=self.win_length,
            hop_length=self.hop_length)
        mag = np.abs(spec)
        mel = librosa.filters.mel(sr=self.sr,
                                  n_fft=self.n_fft,
                                  n_mels=self.n_mels,
                                  fmin=self.fmin,
                                  fmax=self.fmax)
        mel = np.matmul(mel, mag)
        mel = np.log(np.maximum(mel, 1e-5))

        characters = np.array(
            g2p.en.text_to_sequence(normalized_text), dtype=np.int64)
        return (mag, mel, characters)


def batch_examples(batch):
    texts = []
    mels = []
    mel_inputs = []
    text_lens = []
    pos_texts = []
    pos_mels = []
    stop_tokens = []
    for data in batch:
        _, mel, text = data
        mel_inputs.append(
            np.concatenate(
                [np.zeros([mel.shape[0], 1], np.float32), mel[:, :-1]],
                axis=-1))
        text_lens.append(len(text))
        pos_texts.append(np.arange(1, len(text) + 1))
        pos_mels.append(np.arange(1, mel.shape[1] + 1))
        mels.append(mel)
        texts.append(text)
        stop_token = np.append(np.zeros([mel.shape[1] - 1], np.float32), 1.0)
        stop_tokens.append(stop_token)

    # Sort by text_len in descending order
    texts = [
        i
        for i, _ in sorted(
            zip(texts, text_lens), key=lambda x: x[1], reverse=True)
    ]
    mels = [
        i
        for i, _ in sorted(
            zip(mels, text_lens), key=lambda x: x[1], reverse=True)
    ]
    mel_inputs = [
        i
        for i, _ in sorted(
            zip(mel_inputs, text_lens), key=lambda x: x[1], reverse=True)
    ]
    pos_texts = [
        i
        for i, _ in sorted(
            zip(pos_texts, text_lens), key=lambda x: x[1], reverse=True)
    ]
    pos_mels = [
        i
        for i, _ in sorted(
            zip(pos_mels, text_lens), key=lambda x: x[1], reverse=True)
    ]
    stop_tokens = [
        i
        for i, _ in sorted(
            zip(stop_tokens, text_lens), key=lambda x: x[1], reverse=True)
    ]
    text_lens = sorted(text_lens, reverse=True)

    # Pad sequence with largest len of the batch
    texts = TextIDBatcher(pad_id=0)(texts)  #(B, T)
    pos_texts = TextIDBatcher(pad_id=0)(pos_texts)  #(B,T)
    pos_mels = TextIDBatcher(pad_id=0)(pos_mels)  #(B,T)
    stop_tokens = TextIDBatcher(pad_id=1, dtype=np.float32)(pos_mels)
    mels = np.transpose(
        SpecBatcher(pad_value=0.)(mels), axes=(0, 2, 1))  #(B,T,num_mels)
    mel_inputs = np.transpose(
        SpecBatcher(pad_value=0.)(mel_inputs), axes=(0, 2, 1))  #(B,T,num_mels)

    return (texts, mels, mel_inputs, pos_texts, pos_mels, stop_tokens)


def batch_examples_vocoder(batch):
    mels = []
    mags = []
    for data in batch:
        mag, mel, _ = data
        mels.append(mel)
        mags.append(mag)

    mels = np.transpose(SpecBatcher(pad_value=0.)(mels), axes=(0, 2, 1))
    mags = np.transpose(SpecBatcher(pad_value=0.)(mags), axes=(0, 2, 1))

    return (mels, mags)
