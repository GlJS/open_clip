import ast
import json
import logging
import math
import os
import random
import sys
import braceexpand
from dataclasses import dataclass
from multiprocessing import Value

import numpy as np
import pandas as pd
import torch
import torchvision.datasets as datasets
import webdataset as wds
from PIL import Image
from torch.utils.data import Dataset, DataLoader, SubsetRandomSampler, IterableDataset, get_worker_info
from torch.utils.data.distributed import DistributedSampler
from webdataset.filters import _shuffle
from webdataset.tariterators import base_plus_ext, url_opener, tar_file_expander, valid_sample

try:
    import horovod.torch as hvd
except ImportError:
    hvd = None

import ast
import json
import logging
import math
import os
import random
import h5py
from dataclasses import dataclass
import braceexpand
import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
import torchvision.datasets as datasets
import torchvision.transforms
import webdataset as wds
from PIL import Image
from torch.utils.data import Dataset, DataLoader, SubsetRandomSampler
from torch.utils.data.distributed import DistributedSampler
from functools import partial
from pathlib import Path
import wget
import tempfile
import copy
from contextlib import suppress

from open_clip.utils import get_tar_path_from_dataset_name, dataset_split
from open_clip.utils import load_p, load_class_label
from open_clip import tokenize as clip_tokenizer
from transformers import BertTokenizer
from transformers import RobertaTokenizer
from transformers import BartTokenizer

try:
    import horovod.torch as hvd
except ImportError:
    hvd = None

try:
    import torchaudio
except ImportError:
    torchaudio = None

# bert_tokenizer = BertTokenizer.from_pretrained("bert-base-uncased")
# roberta_tokenizer = RobertaTokenizer.from_pretrained("roberta-base")
# bart_tokenizer = BartTokenizer.from_pretrained("facebook/bart-base")

# def tokenizer_fn(text, tmodel="roberta", max_length=77):
#     """tokenizer for different models
#     tmodel is default to roberta as it is the best model for our task
#     max_length is default to 77 from the OpenAI CLIP parameters
#     We assume text to be a single string, but it can also be a list of strings
#     """
#     if tmodel == "transformer":
#         return clip_tokenizer(text).squeeze(0)

#     elif tmodel == "bert":
#         result = bert_tokenizer(
#             text,
#             padding="max_length",
#             truncation=True,
#             max_length=max_length,
#             return_tensors="pt",
#         )
#         return {k: v.squeeze(0) for k, v in result.items()}

#     elif tmodel == "roberta":
#         result = roberta_tokenizer(
#             text,
#             padding="max_length",
#             truncation=True,
#             max_length=max_length,
#             return_tensors="pt",
#         )
#         return {k: v.squeeze(0) for k, v in result.items()}

#     elif tmodel == "bart":
#         result = bart_tokenizer(
#             text,
#             padding="max_length",
#             truncation=True,
#             max_length=max_length,
#             return_tensors="pt",
#         )
#         return {k: v.squeeze(0) for k, v in result.items()}


# initizlied the audioset map
_AUDIOSET_MAP_PATH = os.path.join(Path(__file__).parent, "audioset_textmap.npy")
_AUDIOSET_MAP = np.load(_AUDIOSET_MAP_PATH, allow_pickle=True)


def int16_to_float32(x):
    return (x / 32767.0).astype(np.float32)


def float32_to_int16(x):
    x = np.clip(x, a_min=-1., a_max=1.)
    return (x * 32767.).astype(np.int16)


def int16_to_float32_torch(x):
    return (x / 32767.0).type(torch.float32)


def float32_to_int16_torch(x):
    x = torch.clamp(x, min=-1., max=1.)
    return (x * 32767.).type(torch.int16)


# For Toy Dataset
class ToyDataset(Dataset):
    def __init__(self, index_path, ipc, config, eval_mode=False):
        """Toy Dataset for testing the audioset input with text labels
        Parameters
        ----------
            index_path: str
                the link to the h5 file of each audio
            idc: str
                the link to the npy file, the number of samples in each class
            config: dict
                the audio cfg file
           eval_model (bool): to indicate if the dataset is a testing dataset
        """
        self.audio_cfg = config["audio_cfg"]
        self.text_cfg = config["text_cfg"]
        self.fp = h5py.File(index_path, "r")
        self.ipc = np.load(ipc, allow_pickle=True)
        self.total_size = len(self.fp["audio_name"])
        self.classes_num = self.audio_cfg["class_num"]
        self.eval_mode = eval_mode

        if not eval_mode:
            self.generate_queue()
        else:
            self.queue = []
            for i in range(self.total_size):
                target = self.fp["target"][i]
                if np.sum(target) > 0:
                    self.queue.append(i)
            self.total_size = len(self.queue)
        logging.info("total dataset size: %d" % (self.total_size))
        logging.info("class num: %d" % (self.classes_num))

    def time_shifting(self, x):
        frame_num = len(x)
        shift_len = random.randint(0, frame_num - 1)
        new_sample = np.concatenate([x[shift_len:], x[:shift_len]], axis=0)
        return new_sample

    def generate_queue(self):
        self.queue = []
        while len(self.queue) < self.total_size:
            class_set = [*range(self.classes_num)]
            random.shuffle(class_set)
            self.queue += [
                self.ipc[d][random.randint(0, len(self.ipc[d]) - 1)] for d in class_set
            ]
        self.queue = self.queue[: self.total_size]

        logging.info("queue regenerated:%s" % (self.queue[-5:]))

    def crop_wav(self, x):
        crop_size = self.audio_cfg["crop_size"]
        crop_pos = random.randint(0, len(x) - crop_size - 1)
        return x[crop_pos: crop_pos + crop_size]

    def prompt_text(self, target):
        events = _AUDIOSET_MAP[np.where(target > 0)]
        event_text = "The sounds of " + ", ".join(events[:-1]) + " and " + events[-1]
        text = tokenizer(event_text)[0]
        return text

    def __getitem__(self, index):
        """Load waveform, text, and target of an audio clip

        Parameters
        ----------
            index: int
                the index number
        Return
        ------
            output: dict {
                "hdf5_path": str,
                "index_in_hdf5": int,
                "audio_name": str,
                "waveform": list (audio_length,),
                "target": list (class_num, ),
                "text": torch.tensor (context_length,)
            }
                the output dictionary
        """
        s_index = self.queue[index]

        audio_name = self.fp["audio_name"][s_index].decode()
        # Hardcode here CHANGE
        hdf5_path = (
            self.fp["hdf5_path"][s_index]
            .decode()
            .replace(
                "../workspace",
                "/home/la/kechen/Research/ke_zsasp/workspace",
            )
        )
        r_idx = self.fp["index_in_hdf5"][s_index]
        target = self.fp["target"][s_index].astype(np.float32)
        text = self.prompt_text(target)
        with h5py.File(hdf5_path, "r") as f:
            waveform = int16_to_float32(f["waveform"][r_idx])[
                       : self.audio_cfg["clip_samples"]
                       ]
        assert (
                len(waveform) == self.audio_cfg["clip_samples"]
        ), "The sample length is not match"
        # Time shift
        # if (self.config.enable_time_shift) and (not self.eval_mode):
        #     waveform = self.time_shifting(waveform)
        # # Label Enhance
        # if (self.config.crop_size is not None) and (not self.eval_mode):
        #     waveform = self.crop_wav(waveform)
        # # the label enhance rate is fixed 0.5
        # if (self.config.enable_label_enhance) and (not self.eval_mode) and random.random() < 0.5:
        #     kidx = np.where(target)[0]
        #     for k in kidx:
        #         for add_key in self.class_map[k][1]:
        #             target[add_key] = 1.0
        #         if len(self.class_map[k][2]) > 0:
        #             add_key = random.choice(self.class_map[k][2])
        #             target[add_key] = 1.0

        # missing the text input
        mel_spec = get_mel(torch.from_numpy(waveform), self.audio_cfg)[None, :, :]
        mel_spec = torch.cat([mel_spec, mel_spec.clone(), mel_spec.clone(), mel_spec.clone()], dim=0).cpu().numpy()
        longer = random.choice([True, False])
        if longer == False:
            mel_spec[1:, :, :] = 0.0
        data_dict = {
            "hdf5_path": hdf5_path,
            "index_in_hdf5": r_idx,
            "audio_name": audio_name,
            "waveform": waveform,
            "class_label": target,
            "text": text,
            "longer": longer,
            "mel_fusion": mel_spec
        }
        return data_dict

    def __len__(self):
        return self.total_size

@dataclass
class DataInfo:
    dataloader: DataLoader
    sampler: DistributedSampler


def get_audio_dataset_size(shards, sizefilepath_=None):
    if isinstance(shards, list):
        size_list = []
        for s in shards:
            size_list.append(
                get_audio_dataset_size(s, sizefilepath_=sizefilepath_)[0]
            )
    else:
       
        shards_list = list(braceexpand.braceexpand(shards))
        dir_path = os.path.dirname(shards)
        if sizefilepath_ is not None:
            sizes = json.load(open(sizefilepath_, "r"))
            total_size = sum(
                [
                    int(sizes[os.path.basename(shard.replace(".tar -", ".tar"))])
                    for shard in shards_list
                ]
            )
        else:
            sizes_filename = os.path.join(dir_path, "sizes.json")
            len_filename = os.path.join(dir_path, "__len__")
            if os.path.exists(sizes_filename):
                sizes = json.load(open(sizes_filename, "r"))
                total_size = sum(
                    [int(sizes[os.path.basename(shard)]) for shard in shards_list]
                )
            elif os.path.exists(len_filename):
                # FIXME this used to be eval(open(...)) but that seemed rather unsafe
                total_size = ast.literal_eval(open(len_filename, "r").read())
            else:
                raise Exception(
                    f"Cannot find sizes file for dataset {shards}. Please specify the path to the file."
                )
                # total_size = None  # num samples undefined
                # some common dataset sizes (at time of authors last download)
                # cc3m-train: 2905954
                # cc12m: 10968539
                # LAION-400m: 407332084
        num_shards = len(shards_list)
    if isinstance(shards, list):
        return sum(size_list), len(shards)
    else:
        return total_size, num_shards


def count_samples(dataloader):
    os.environ["WDS_EPOCH"] = "0"
    n_elements, n_batches = 0, 0
    for images, texts in dataloader:
        n_batches += 1
        n_elements += len(images)
        assert len(images) == len(texts)
    return n_elements, n_batches


def log_and_continue(exn):
    """Call in an exception handler to ignore any exception, isssue a warning, and continue."""
    logging.warning(f"Handling webdataset error ({repr(exn)}). Ignoring.")
    return True


_SHARD_SHUFFLE_SIZE = 2000
_SHARD_SHUFFLE_INITIAL = 500
_SAMPLE_SHUFFLE_SIZE = 5000
_SAMPLE_SHUFFLE_INITIAL = 1000


def sample_prop(sizefile, inputs, proportion):
    """
    Sample a proportion of the data.
    """
    file_path_dict = {
        os.path.split(inputs[i])[1]: os.path.split(inputs[i])[0]
        for i in range(len(inputs))
    }
    sampled_filepath_dict = {}
    sampled_size_dict = {}
    
    with open(sizefile, "r", encoding="UTF-8") as f:
        load_dict = json.load(f)
    L = int(len(file_path_dict) * proportion)
    subkeys = random.sample(file_path_dict.keys(), L)
    for k in subkeys:
        sampled_size_dict[k] = load_dict[k]
        sampled_filepath_dict[k] = file_path_dict[k]
    return (
        sum(sampled_size_dict.values()),
        L,
        [os.path.join(v, k) for k, v in sampled_filepath_dict.items()],
        sampled_size_dict,
    )


def get_mel(audio_data, audio_cfg):
    # mel shape: (n_mels, T)
    mel_tf = torchaudio.transforms.MelSpectrogram(
        sample_rate=audio_cfg['sample_rate'],
        n_fft=audio_cfg['window_size'],
        win_length=audio_cfg['window_size'],
        hop_length=audio_cfg['hop_size'],
        center=True,
        pad_mode="reflect",
        power=2.0,
        norm=None,
        onesided=True,
        n_mels=audio_cfg['mel_bins'],
        f_min=audio_cfg['fmin'],
        f_max=audio_cfg['fmax']
    ).to(audio_data.device)
    
    mel = mel_tf(audio_data)
    # Align to librosa:
    # librosa_melspec = librosa.feature.melspectrogram(
    #     waveform,
    #     sr=audio_cfg['sample_rate'],
    #     n_fft=audio_cfg['window_size'],
    #     hop_length=audio_cfg['hop_size'],
    #     win_length=audio_cfg['window_size'],
    #     center=True,
    #     pad_mode="reflect",
    #     power=2.0,
    #     n_mels=audio_cfg['mel_bins'],
    #     norm=None,
    #     htk=True,
    #     f_min=audio_cfg['fmin'],
    #     f_max=audio_cfg['fmax']
    # )
    # we use log mel spectrogram as input
    mel = torchaudio.transforms.AmplitudeToDB(top_db=None)(mel)
    return mel.T  # (T, n_mels)


def get_audio_features(sample, audio_data, max_len, data_truncating, data_filling, audio_cfg, require_grad=False):
    """
    Calculate and add audio features to sample.
    Sample: a dict containing all the data of current sample.
    audio_data: a tensor of shape (T) containing audio data.
    max_len: the maximum length of audio data.
    data_truncating: the method of truncating data.
    data_filling: the method of filling data.
    audio_cfg: a dict containing audio configuration. Comes from model_cfg['audio_cfg'].
    require_grad: whether to require gradient for audio data.
        This is useful when we want to apply gradient-based classifier-guidance.
    """
    grad_fn = suppress if require_grad else torch.no_grad
    with grad_fn():
        if len(audio_data) > max_len:
            if data_truncating == "rand_trunc":
                longer = torch.tensor([True])
            elif data_truncating == "fusion":
                # fusion
                mel = get_mel(audio_data, audio_cfg)
                # split to three parts
                chunk_frames = max_len // audio_cfg['hop_size'] + 1  # the +1 related to how the spectrogram is computed
                total_frames = mel.shape[0]
                if chunk_frames == total_frames:
                    # there is a corner case where the audio length is
                    # larger than max_len but smaller than max_len+hop_size.
                    # In this case, we just use the whole audio.
                    mel_fusion = torch.stack([mel, mel, mel, mel], dim=0)
                    sample["mel_fusion"] = mel_fusion
                    longer = torch.tensor([False])
                else:
                    ranges = np.array_split(list(range(0, total_frames - chunk_frames + 1)), 3)
                    # print('total_frames-chunk_frames:', total_frames-chunk_frames,
                    #       'len(audio_data):', len(audio_data),
                    #       'chunk_frames:', chunk_frames,
                    #       'total_frames:', total_frames)
                    if len(ranges[1]) == 0:
                        # if the audio is too short, we just use the first chunk
                        ranges[1] = [0]
                    if len(ranges[2]) == 0:
                        # if the audio is too short, we just use the first chunk
                        ranges[2] = [0]
                    # randomly choose index for each part
                    idx_front = np.random.choice(ranges[0])
                    idx_middle = np.random.choice(ranges[1])
                    idx_back = np.random.choice(ranges[2])
                    # select mel
                    mel_chunk_front = mel[idx_front:idx_front + chunk_frames, :]
                    mel_chunk_middle = mel[idx_middle:idx_middle + chunk_frames, :]
                    mel_chunk_back = mel[idx_back:idx_back + chunk_frames, :]

                    # shrink the mel
                    mel_shrink = torchvision.transforms.Resize(size=[chunk_frames, audio_cfg['mel_bins']])(mel[None])[0]
                    # logging.info(f"mel_shrink.shape: {mel_shrink.shape}")

                    # stack
                    mel_fusion = torch.stack([mel_shrink, mel_chunk_front, mel_chunk_middle, mel_chunk_back], dim=0)
                    sample["mel_fusion"] = mel_fusion
                    longer = torch.tensor([True])
            else:
                raise NotImplementedError(
                    f"data_truncating {data_truncating} not implemented"
                )
            # random crop to max_len (for compatibility)
            overflow = len(audio_data) - max_len
            idx = np.random.randint(0, overflow + 1)
            audio_data = audio_data[idx: idx + max_len]

        else:  # padding if too short
            if len(audio_data) < max_len:  # do nothing if equal
                if data_filling == "repeatpad":
                    n_repeat = int(max_len / len(audio_data))
                    audio_data = audio_data.repeat(n_repeat)
                    # audio_data = audio_data.unsqueeze(0).unsqueeze(0).unsqueeze(0)
                    # audio_data = F.interpolate(audio_data,size=max_len,mode="bicubic")[0,0,0]
                    audio_data = F.pad(
                        audio_data,
                        (0, max_len - len(audio_data)),
                        mode="constant",
                        value=0,
                    )
                elif data_filling == "pad":
                    audio_data = F.pad(
                        audio_data,
                        (0, max_len - len(audio_data)),
                        mode="constant",
                        value=0,
                    )
                elif data_filling == "repeat":
                    n_repeat = int(max_len / len(audio_data))
                    audio_data = audio_data.repeat(n_repeat + 1)[:max_len]
                else:
                    raise NotImplementedError(
                        f"data_filling {data_filling} not implemented"
                    )
            if data_truncating == 'fusion':
                mel = get_mel(audio_data, audio_cfg)
                mel_fusion = torch.stack([mel, mel, mel, mel], dim=0)
                sample["mel_fusion"] = mel_fusion
            longer = torch.tensor([False])

    sample["longer"] = longer
    sample["waveform"] = audio_data

    return sample


# def select_text(json_dict_raw, text_augment_selection):
#     # For selecting augmented text from dataset
#     if text_augment_selection is None or text_augment_selection == "none":
#         texts = json_dict_raw["text"]
#     elif text_augment_selection == "all":
#         if "text_augment_all" in json_dict_raw.keys():
#             texts = json_dict_raw["text_augment_all"]
#         else:
#             texts = json_dict_raw["text"]
#     elif text_augment_selection == "augment_only":
#         if "text_augment_all" in json_dict_raw.keys():
#             if json_dict_raw["text_augment_t5"] is None:
#                 texts = json_dict_raw["text"]
#             else:
#                 texts = json_dict_raw["text_augment_t5"]
#         else:
#             texts = json_dict_raw["text"]
#     else:
#         raise NotImplementedError(
#             f"text_augment_selection {text_augment_selection} not implemented"
#         )
#     return texts


def preprocess_single(
        sample,
        audio_ext,
        text_ext,
        max_len,
        audio_cfg,
        class_index_dict,
        data_filling,
        data_truncating,
        tokenizer
):
    """
    Preprocess a single sample for wdsdataloader.
    """
    audio_data, orig_sr = sample[audio_ext]
    audio_data = int16_to_float32_torch(float32_to_int16_torch(audio_data[0]))

    sample = get_audio_features(
            sample, 
            audio_data, 
            max_len=max_len, 
            data_truncating=data_truncating, 
            data_filling=data_filling, 
            audio_cfg=audio_cfg
        )
    del sample[audio_ext]

    json_dict_raw = sample[text_ext]

    texts = json_dict_raw["text"]

    # texts = select_text(json_dict_raw, text_augment_selection)
    sample["full_text"] = texts

    if isinstance(texts, list) and isinstance(texts[0], str) and len(texts) > 1:
        texts = random.choice(texts)
    sample["raw_text"] = texts
    sample["text"] = tokenizer(texts).squeeze(0)  # text shape: [num_token]
    
    if class_index_dict is not None:
        # https://stackoverflow.com/questions/48004243/how-to-share-large-read-only-dictionary-list-across-processes-in-multiprocessing
        # https://stackoverflow.com/questions/45693949/storing-strings-in-a-multiprocessing-sharedctypes-array

        # in case the re-written version is wrong, here is the old version:
        # sample["class_label"] = np.zeros(len(class_index_dict.keys()))
        # for x in json_dict_raw["tag"]:
        #     sample["class_label"][class_index_dict[x]] = 1
        # sample["class_label"] = torch.tensor(sample["class_label"]).float()

        class_labels = np.zeros(len(class_index_dict))
        class_labels[np.in1d(list(class_index_dict.keys()), json_dict_raw["tag"])] = 1
        sample["class_label"] = torch.tensor(class_labels).float()

    del sample[text_ext]
    sample["audio_name"] = sample["__key__"].split("/")[-1] + "." + audio_ext
    sample["text_name"] = sample["__key__"].split("/")[-1] + "." + text_ext
    sample["audio_orig_sr"] = orig_sr
    return sample


def collate_fn_with_preprocess(batch,
                               audio_ext,
                               text_ext,
                               max_len,
                               audio_cfg,
                               args,
                               tokenizer
                               ):
    """
    Collate function for wdsdataloader.
    batch: a list of dict, each dict is a sample
    """

    class_index_dict = copy.deepcopy(args.class_index_dict)  # To avoid deadlock in multiprocessing
    data_filling = args.data_filling
    data_truncating = args.data_truncating
    # text_augment_selection = args.text_augment_selection
    # tmodel = args.tmodel

    # concatenate values in each dictionary. if it is a tensor, concatenate. if it is a list, extend.
    data_preprocessed = []

    for sample in batch:
        data_preprocessed.append(
            preprocess_single(sample, audio_ext, text_ext, max_len, audio_cfg, class_index_dict, data_filling,
                              data_truncating, tokenizer))

    batch_dict = {}
    for k in data_preprocessed[0].keys():
        if isinstance(data_preprocessed[0][k], dict):  # dealwith bert tokenizer output
            batch_dict[k] = {}
            for kk in data_preprocessed[0][k].keys():
                tmp = []
                for i in range(len(data_preprocessed)):
                    tmp.append(data_preprocessed[i][k][kk])
                batch_dict[k][kk] = torch.vstack(tmp)
        elif isinstance(data_preprocessed[0][k], torch.Tensor):
            batch_dict[k] = torch.stack([sample[k] for sample in data_preprocessed])
        elif isinstance(data_preprocessed[0][k], np.ndarray):
            batch_dict[k] = torch.tensor(np.stack([sample[k] for sample in data_preprocessed]))
        else:
            batch_dict[k] = [sample[k] for sample in data_preprocessed]
    del data_preprocessed
    return batch_dict


def get_audio_wds_dataset(
        args,
        preprocess_fns,
        is_train,
        epoch=0,
        audio_ext="flac",
        text_ext="json",
        max_len=480000,
        proportion=1.0,
        sizefilepath_=None,
        tokenizer=None
):
    """
    Get a dataset for wdsdataloader.
    """
    args.class_index_dict = load_class_label(args.class_label_path)
    # if is_local is None and (not args.remotedata is None):
    #     is_local = not args.remotedata
    model_cfg = args.model_cfg
    input_shards = args.train_data if is_train else args.val_data
    assert input_shards is not None

    if not sizefilepath_ is None:
        sizefilepath = sizefilepath_
    else:
        sizefilepath = os.path.join(os.path.dirname(input_shards[0]), "sizes.json")
    logging.info(sizefilepath)

    if proportion != 1.0:
        num_samples, num_shards, input_shards, _ = sample_prop(
            sizefilepath, input_shards, proportion
        )
        
    else:
        num_samples, num_shards = get_audio_dataset_size(
            input_shards, sizefilepath_=sizefilepath_
        )

    # logging.info(num_samples, num_shards, input_shards)
    # logging.info("sizefilepath_", sizefilepath_)

    if not num_samples:
        if is_train:
            num_samples = args.train_num_samples
            if not num_samples:
                raise RuntimeError(
                    "Currently, number of dataset samples must be specified for training dataset. "
                    "Please specify via `--train-num-samples` if no dataset length info present."
                )
        else:
            num_samples = (
                    args.val_num_samples or 0
            )  # eval will just exhaust the iterator if not specified
    shared_epoch = SharedEpoch(epoch=epoch)
    pipeline = [wds.SimpleShardList(input_shards)]
    # at this point we have an iterator over all the shards
    # TODO: (yusong): add a if statement of distributed. If not, we don't need to split_by_node
    if is_train or args.parallel_eval:
        pipeline.extend(
            [
                detshuffle2(
                    bufsize=_SHARD_SHUFFLE_SIZE,
                    initial=_SHARD_SHUFFLE_INITIAL,
                    seed=args.seed,
                    epoch=shared_epoch
                ),
                wds.split_by_node,
                wds.split_by_worker,
                # at this point, we have an iterator over the shards assigned to each worker at each node
                wds.tarfile_to_samples(handler=log_and_continue),
                wds.shuffle(
                    bufsize=_SAMPLE_SHUFFLE_SIZE,
                    initial=_SAMPLE_SHUFFLE_INITIAL,
                    rng=random.Random(args.seed),
                ),
                # wds.repeatedly,  # FIXME determine if this is beneficial
            ]
        )
    else:
        pipeline.extend(
            [
                wds.split_by_worker,
                # at this point, we have an iterator over the shards assigned to each worker
                wds.tarfile_to_samples(handler=log_and_continue),
            ]
        )

    pipeline.append(
        wds.decode(wds.torch_audio),
    )

    pipeline.append(
        wds.batched(
            args.batch_size,
            partial=not (is_train or args.parallel_eval),
            collation_fn=partial(collate_fn_with_preprocess,
                                 audio_ext=audio_ext,
                                 text_ext=text_ext,
                                 max_len=max_len,
                                 audio_cfg=model_cfg['audio_cfg'],
                                 args=args,
                                 tokenizer=tokenizer
                                 ),

        )
    )

    dataset = wds.DataPipeline(*pipeline)
    # if is_train or args.parallel_eval:
    #     # (yusong): Currently parallel evaluation will be not precise as we are repeat the last few samples.
    #     # (yusong): See comments below.
    #     # roll over and repeat a few samples to get same number of full batches on each node
    #     global_batch_size = args.batch_size * args.world_size
    #     num_batches = math.ceil(num_samples / global_batch_size)
    #     num_workers = max(1, args.workers)
    #     num_worker_batches = math.ceil(
    #         num_batches / num_workers
    #     )  # per dataloader worker
    #     num_batches = num_worker_batches * num_workers
    #     num_samples = num_batches * global_batch_size
    #     print("num samples", num_samples)
    #     print("num_batches", num_batches)
    #     print("num_worker_batches", num_worker_batches)
    #     print("global_batch_size", global_batch_size)
    #     print("rgs.world_size", args.world_size)
    #     dataset = dataset.with_epoch(
    #         num_worker_batches
    #     )  # each worker is iterating over this
    # else:
    #     # last batches are partial, eval is done on single (master) node
    #     num_batches = math.ceil(num_samples / args.batch_size)

    # kwargs = {}
    # if args.horovod:  # multi-node training on summit
    #     kwargs["multiprocessing_context"] = "forkserver"

    # if is_train:
    #     # if args.prefetch_factor:
    #     #     prefetch_factor = args.prefetch_factor
    #     # else:
    #     prefetch_factor = max(2, args.batch_size // args.workers)
    # else:
    #     prefetch_factor = 2

    # dataloader = wds.WebLoader(
    #     dataset,
    #     batch_size=None,
    #     shuffle=False,
    #     num_workers=args.workers,
    #     pin_memory=True,
    #     prefetch_factor=prefetch_factor,
    #     **kwargs
    # )

    # # FIXME not clear which approach is better, with_epoch before vs after dataloader?
    # # hoping to resolve via https://github.com/webdataset/webdataset/issues/169
    # # if is_train:
    # #     # roll over and repeat a few samples to get same number of full batches on each node
    # #     global_batch_size = args.batch_size * args.world_size
    # #     num_batches = math.ceil(num_samples / global_batch_size)
    # #     num_workers = max(1, args.workers)
    # #     num_batches = math.ceil(num_batches / num_workers) * num_workers
    # #     num_samples = num_batches * global_batch_size
    # #     dataloader = dataloader.with_epoch(num_batches)
    # # else:
    # #     # last batches are partial, eval is done on single (master) node
    # #     num_batches = math.ceil(num_samples / args.batch_size)

    # # add meta-data to dataloader instance for convenience
    # dataloader.num_batches = num_batches
    # dataloader.num_samples = num_samples

    # return DataInfo(dataloader, shared_epoch=shared_epoch)

    if is_train:
        # if not resampled:
        #     num_shards = num_shards or len(expand_urls(input_shards)[0])
        #     assert num_shards >= args.workers * args.world_size, 'number of shards must be >= total workers'
        # roll over and repeat a few samples to get same number of full batches on each node
        round_fn = math.ceil
        global_batch_size = args.batch_size * args.world_size
        num_batches = round_fn(num_samples / global_batch_size)
        num_workers = max(1, args.workers)
        num_worker_batches = round_fn(num_batches / num_workers)  # per dataloader worker
        num_batches = num_worker_batches * num_workers
        num_samples = num_batches * global_batch_size
        dataset = dataset.with_epoch(num_worker_batches)  # each worker is iterating over this
    else:
        # last batches are partial, eval is done on single (master) node
        num_batches = math.ceil(num_samples / args.batch_size)

    dataloader = wds.WebLoader(
        dataset,
        batch_size=None,
        shuffle=False,
        num_workers=args.workers,
        persistent_workers=args.workers > 0,
    )

    dataloader.num_batches = num_batches
    dataloader.num_samples = num_samples

    return DataInfo(dataloader=dataloader, shared_epoch=shared_epoch)

class CsvDataset(Dataset):
    def __init__(self, input_filename, transforms, img_key, caption_key, sep="\t", tokenizer=None):
        logging.debug(f'Loading csv data from {input_filename}.')
        df = pd.read_csv(input_filename, sep=sep)

        self.images = df[img_key].tolist()
        self.captions = df[caption_key].tolist()
        self.transforms = transforms
        logging.debug('Done loading data.')

        self.tokenize = tokenizer

    def __len__(self):
        return len(self.captions)

    def __getitem__(self, idx):
        images = self.transforms(Image.open(str(self.images[idx])))
        texts = self.tokenize([str(self.captions[idx])])[0]
        return images, texts


class SharedEpoch:
    def __init__(self, epoch: int = 0):
        self.shared_epoch = Value('i', epoch)

    def set_value(self, epoch):
        self.shared_epoch.value = epoch

    def get_value(self):
        return self.shared_epoch.value


@dataclass
class DataInfo:
    dataloader: DataLoader
    sampler: DistributedSampler = None
    shared_epoch: SharedEpoch = None

    def set_epoch(self, epoch):
        if self.shared_epoch is not None:
            self.shared_epoch.set_value(epoch)
        if self.sampler is not None and isinstance(self.sampler, DistributedSampler):
            self.sampler.set_epoch(epoch)


def expand_urls(urls, weights=None):
    if weights is None:
        expanded_urls = wds.shardlists.expand_urls(urls)
        return expanded_urls, None
    if isinstance(urls, str):
        urllist = urls.split("::")
        weights = weights.split('::')
        assert len(weights) == len(urllist),\
            f"Expected the number of data components ({len(urllist)}) and weights({len(weights)}) to match."
        weights = [float(weight) for weight in weights]
        all_urls, all_weights = [], []
        for url, weight in zip(urllist, weights):
            expanded_url = list(braceexpand.braceexpand(url))
            expanded_weights = [weight for _ in expanded_url]
            all_urls.extend(expanded_url)
            all_weights.extend(expanded_weights)
        return all_urls, all_weights
    else:
        all_urls = list(urls)
        return all_urls, weights


def get_dataset_size(shards):
    shards_list, _ = expand_urls(shards)
    dir_path = os.path.dirname(shards_list[0])
    sizes_filename = os.path.join(dir_path, 'sizes.json')
    len_filename = os.path.join(dir_path, '__len__')
    if os.path.exists(sizes_filename):
        sizes = json.load(open(sizes_filename, 'r'))
        total_size = sum([int(sizes[os.path.basename(shard)]) for shard in shards_list])
    elif os.path.exists(len_filename):
        # FIXME this used to be eval(open(...)) but that seemed rather unsafe
        total_size = ast.literal_eval(open(len_filename, 'r').read())
    else:
        total_size = None  # num samples undefined
        # some common dataset sizes (at time of authors last download)
        # CC3M (train): 2905954
        # CC12M: 10968539
        # LAION-400M: 407332084
        # LAION-2B (english): 2170337258
    num_shards = len(shards_list)
    return total_size, num_shards


def get_imagenet(args, preprocess_fns, split):
    assert split in ["train", "val", "v2"]
    is_train = split == "train"
    preprocess_train, preprocess_val = preprocess_fns

    if split == "v2":
        from imagenetv2_pytorch import ImageNetV2Dataset
        dataset = ImageNetV2Dataset(location=args.imagenet_v2, transform=preprocess_val)
    else:
        if is_train:
            data_path = args.imagenet_train
            preprocess_fn = preprocess_train
        else:
            data_path = args.imagenet_val
            preprocess_fn = preprocess_val
        assert data_path

        dataset = datasets.ImageFolder(data_path, transform=preprocess_fn)

    if is_train:
        idxs = np.zeros(len(dataset.targets))
        target_array = np.array(dataset.targets)
        k = 50
        for c in range(1000):
            m = target_array == c
            n = len(idxs[m])
            arr = np.zeros(n)
            arr[:k] = 1
            np.random.shuffle(arr)
            idxs[m] = arr

        idxs = idxs.astype('int')
        sampler = SubsetRandomSampler(np.where(idxs)[0])
    else:
        sampler = None

    dataloader = torch.utils.data.DataLoader(
        dataset,
        batch_size=args.batch_size,
        num_workers=args.workers,
        sampler=sampler,
    )

    return DataInfo(dataloader=dataloader, sampler=sampler)


def count_samples(dataloader):
    os.environ["WDS_EPOCH"] = "0"
    n_elements, n_batches = 0, 0
    for images, texts in dataloader:
        n_batches += 1
        n_elements += len(images)
        assert len(images) == len(texts)
    return n_elements, n_batches


def filter_no_caption_or_no_image(sample):
    has_caption = ('txt' in sample)
    has_image = ('png' in sample or 'jpg' in sample or 'jpeg' in sample or 'webp' in sample)
    return has_caption and has_image


def log_and_continue(exn):
    """Call in an exception handler to ignore any exception, issue a warning, and continue."""
    logging.warning(f'Handling webdataset error ({repr(exn)}). Ignoring.')
    return True


def group_by_keys_nothrow(data, keys=base_plus_ext, lcase=True, suffixes=None, handler=None):
    """Return function over iterator that groups key, value pairs into samples.

    :param keys: function that splits the key into key and extension (base_plus_ext)
    :param lcase: convert suffixes to lower case (Default value = True)
    """
    current_sample = None
    for filesample in data:
        assert isinstance(filesample, dict)
        fname, value = filesample["fname"], filesample["data"]
        prefix, suffix = keys(fname)
        if prefix is None:
            continue
        if lcase:
            suffix = suffix.lower()
        # FIXME webdataset version throws if suffix in current_sample, but we have a potential for
        #  this happening in the current LAION400m dataset if a tar ends with same prefix as the next
        #  begins, rare, but can happen since prefix aren't unique across tar files in that dataset
        if current_sample is None or prefix != current_sample["__key__"] or suffix in current_sample:
            if valid_sample(current_sample):
                yield current_sample
            current_sample = dict(__key__=prefix, __url__=filesample["__url__"])
        if suffixes is None or suffix in suffixes:
            current_sample[suffix] = value
    if valid_sample(current_sample):
        yield current_sample


def tarfile_to_samples_nothrow(src, handler=log_and_continue):
    # NOTE this is a re-impl of the webdataset impl with group_by_keys that doesn't throw
    streams = url_opener(src, handler=handler)
    files = tar_file_expander(streams, handler=handler)
    samples = group_by_keys_nothrow(files, handler=handler)
    return samples


def pytorch_worker_seed(increment=0):
    """get dataloader worker seed from pytorch"""
    worker_info = get_worker_info()
    if worker_info is not None:
        # favour using the seed already created for pytorch dataloader workers if it exists
        seed = worker_info.seed
        if increment:
            # space out seed increments so they can't overlap across workers in different iterations
            seed += increment * max(1, worker_info.num_workers)
        return seed
    # fallback to wds rank based seed
    return wds.utils.pytorch_worker_seed()


_SHARD_SHUFFLE_SIZE = 2000
_SHARD_SHUFFLE_INITIAL = 500
_SAMPLE_SHUFFLE_SIZE = 5000
_SAMPLE_SHUFFLE_INITIAL = 1000


class detshuffle2(wds.PipelineStage):
    def __init__(
            self,
            bufsize=1000,
            initial=100,
            seed=0,
            epoch=-1,
    ):
        self.bufsize = bufsize
        self.initial = initial
        self.seed = seed
        self.epoch = epoch

    def run(self, src):
        if isinstance(self.epoch, SharedEpoch):
            epoch = self.epoch.get_value()
        else:
            # NOTE: this is epoch tracking is problematic in a multiprocess (dataloader workers or train)
            # situation as different workers may wrap at different times (or not at all).
            self.epoch += 1
            epoch = self.epoch
        rng = random.Random()
        if self.seed < 0:
            # If seed is negative, we use the worker's seed, this will be different across all nodes/workers
            seed = pytorch_worker_seed(epoch)
        else:
            # This seed to be deterministic AND the same across all nodes/workers in each epoch
            seed = self.seed + epoch
        rng.seed(seed)
        return _shuffle(src, self.bufsize, self.initial, rng)


class ResampledShards2(IterableDataset):
    """An iterable dataset yielding a list of urls."""

    def __init__(
        self,
        urls,
        weights=None,
        nshards=sys.maxsize,
        worker_seed=None,
        deterministic=False,
        epoch=-1,
    ):
        """Sample shards from the shard list with replacement.

        :param urls: a list of URLs as a Python list or brace notation string
        """
        super().__init__()
        urls, weights = expand_urls(urls, weights)
        self.urls = urls
        self.weights = weights
        if self.weights is not None:
            assert len(self.urls) == len(self.weights),\
                f"Number of urls {len(self.urls)} and weights {len(self.weights)} should match."
        assert isinstance(self.urls[0], str)
        self.nshards = nshards
        self.rng = random.Random()
        self.worker_seed = worker_seed
        self.deterministic = deterministic
        self.epoch = epoch

    def __iter__(self):
        """Return an iterator over the shards."""
        if isinstance(self.epoch, SharedEpoch):
            epoch = self.epoch.get_value()
        else:
            # NOTE: this is epoch tracking is problematic in a multiprocess (dataloader workers or train)
            # situation as different workers may wrap at different times (or not at all).
            self.epoch += 1
            epoch = self.epoch
        if self.deterministic:
            # reset seed w/ epoch if deterministic
            if self.worker_seed is None:
                # pytorch worker seed should be deterministic due to being init by arg.seed + rank + worker id
                seed = pytorch_worker_seed(epoch)
            else:
                seed = self.worker_seed() + epoch
            self.rng.seed(seed)
        for _ in range(self.nshards):
            if self.weights is None:
                yield dict(url=self.rng.choice(self.urls))
            else:
                yield dict(url=self.rng.choices(self.urls, weights=self.weights, k=1)[0])


def get_wds_dataset(args, preprocess_img, is_train, epoch=0, floor=False, tokenizer=None):
    input_shards = args.train_data if is_train else args.val_data
    assert input_shards is not None
    resampled = getattr(args, 'dataset_resampled', False) and is_train

    num_shards = None
    if is_train:
        if args.train_num_samples is not None:
            num_samples = args.train_num_samples
        else:
            num_samples, num_shards = get_dataset_size(input_shards)
            if not num_samples:
                raise RuntimeError(
                    'Currently, the number of dataset samples must be specified for the training dataset. '
                    'Please specify it via `--train-num-samples` if no dataset length info is present.')
    else:
        # Eval will just exhaust the iterator if the size is not specified.
        num_samples = args.val_num_samples or 0 

    shared_epoch = SharedEpoch(epoch=epoch)  # create a shared epoch store to sync epoch to dataloader worker proc

    if is_train and args.train_data_upsampling_factors is not None:
        assert resampled, "--train_data_upsampling_factors is only supported when sampling with replacement (with --dataset-resampled)."
    
    if resampled:
        pipeline = [ResampledShards2(
            input_shards,
            weights=args.train_data_upsampling_factors,
            deterministic=True,
            epoch=shared_epoch,
        )]
    else:
        pipeline = [wds.SimpleShardList(input_shards)]

    # at this point we have an iterator over all the shards
    if is_train:
        if not resampled:
            pipeline.extend([
                detshuffle2(
                    bufsize=_SHARD_SHUFFLE_SIZE,
                    initial=_SHARD_SHUFFLE_INITIAL,
                    seed=args.seed,
                    epoch=shared_epoch,
                ),
                wds.split_by_node,
                wds.split_by_worker,
            ])
        pipeline.extend([
            # at this point, we have an iterator over the shards assigned to each worker at each node
            tarfile_to_samples_nothrow,  # wds.tarfile_to_samples(handler=log_and_continue),
            wds.shuffle(
                bufsize=_SAMPLE_SHUFFLE_SIZE,
                initial=_SAMPLE_SHUFFLE_INITIAL,
            ),
        ])
    else:
        pipeline.extend([
            wds.split_by_worker,
            # at this point, we have an iterator over the shards assigned to each worker
            wds.tarfile_to_samples(handler=log_and_continue),
        ])
    pipeline.extend([
        wds.select(filter_no_caption_or_no_image),
        wds.decode("pilrgb", handler=log_and_continue),
        wds.rename(image="jpg;png;jpeg;webp", text="txt"),
        wds.map_dict(image=preprocess_img, text=lambda text: tokenizer(text)[0]),
        wds.to_tuple("image", "text"),
        wds.batched(args.batch_size, partial=not is_train)
    ])

    dataset = wds.DataPipeline(*pipeline)

    if is_train:
        if not resampled:
            num_shards = num_shards or len(expand_urls(input_shards)[0])
            assert num_shards >= args.workers * args.world_size, 'number of shards must be >= total workers'
        # roll over and repeat a few samples to get same number of full batches on each node
        round_fn = math.floor if floor else math.ceil
        global_batch_size = args.batch_size * args.world_size
        num_batches = round_fn(num_samples / global_batch_size)
        num_workers = max(1, args.workers)
        num_worker_batches = round_fn(num_batches / num_workers)  # per dataloader worker
        num_batches = num_worker_batches * num_workers
        num_samples = num_batches * global_batch_size
        dataset = dataset.with_epoch(num_worker_batches)  # each worker is iterating over this
    else:
        # last batches are partial, eval is done on single (master) node
        num_batches = math.ceil(num_samples / args.batch_size)

    dataloader = wds.WebLoader(
        dataset,
        batch_size=None,
        shuffle=False,
        num_workers=args.workers,
        persistent_workers=args.workers > 0,
    )

    # FIXME not clear which approach is better, with_epoch before vs after dataloader?
    # hoping to resolve via https://github.com/webdataset/webdataset/issues/169
    # if is_train:
    #     # roll over and repeat a few samples to get same number of full batches on each node
    #     global_batch_size = args.batch_size * args.world_size
    #     num_batches = math.ceil(num_samples / global_batch_size)
    #     num_workers = max(1, args.workers)
    #     num_batches = math.ceil(num_batches / num_workers) * num_workers
    #     num_samples = num_batches * global_batch_size
    #     dataloader = dataloader.with_epoch(num_batches)
    # else:
    #     # last batches are partial, eval is done on single (master) node
    #     num_batches = math.ceil(num_samples / args.batch_size)

    # add meta-data to dataloader instance for convenience
    dataloader.num_batches = num_batches
    dataloader.num_samples = num_samples

    return DataInfo(dataloader=dataloader, shared_epoch=shared_epoch)


def get_csv_dataset(args, preprocess_fn, is_train, epoch=0, tokenizer=None):
    input_filename = args.train_data if is_train else args.val_data
    assert input_filename
    dataset = CsvDataset(
        input_filename,
        preprocess_fn,
        img_key=args.csv_img_key,
        caption_key=args.csv_caption_key,
        sep=args.csv_separator,
        tokenizer=tokenizer
    )
    num_samples = len(dataset)
    sampler = DistributedSampler(dataset) if args.distributed and is_train else None
    shuffle = is_train and sampler is None

    dataloader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=shuffle,
        num_workers=args.workers,
        pin_memory=True,
        sampler=sampler,
        drop_last=is_train,
    )
    dataloader.num_samples = num_samples
    dataloader.num_batches = len(dataloader)

    return DataInfo(dataloader, sampler)


class SyntheticDataset(Dataset):

    def __init__(
            self,
            transform=None,
            image_size=(224, 224),
            caption="Dummy caption",
            dataset_size=100,
            tokenizer=None,
    ):
        self.transform = transform
        self.image_size = image_size
        self.caption = caption
        self.image = Image.new('RGB', image_size)
        self.dataset_size = dataset_size

        self.preprocess_txt = lambda text: tokenizer(text)[0]

    def __len__(self):
        return self.dataset_size

    def __getitem__(self, idx):
        if self.transform is not None:
            image = self.transform(self.image)
        return image, self.preprocess_txt(self.caption)


def get_synthetic_dataset(args, preprocess_fn, is_train, epoch=0, tokenizer=None):
    image_size = preprocess_fn.transforms[0].size
    dataset = SyntheticDataset(
        transform=preprocess_fn, image_size=image_size, dataset_size=args.train_num_samples, tokenizer=tokenizer)
    num_samples = len(dataset)
    sampler = DistributedSampler(dataset) if args.distributed and is_train else None
    shuffle = is_train and sampler is None

    dataloader = DataLoader(
        dataset,
        batch_size=args.batch_size,
        shuffle=shuffle,
        num_workers=args.workers,
        pin_memory=True,
        sampler=sampler,
        drop_last=is_train,
    )
    dataloader.num_samples = num_samples
    dataloader.num_batches = len(dataloader)

    return DataInfo(dataloader, sampler)


def get_dataset_fn(data_path, dataset_type):
    if dataset_type == "webdataset":
        return get_wds_dataset
    elif dataset_type == "csv":
        return get_csv_dataset
    elif dataset_type == "synthetic":
        return get_synthetic_dataset
    elif dataset_type == "auto":
        ext = data_path.split('.')[-1]
        if ext in ['csv', 'tsv']:
            return get_csv_dataset
        elif ext in ['tar']:
            return get_wds_dataset
        else:
            raise ValueError(
                f"Tried to figure out dataset type, but failed for extension {ext}.")

    elif dataset_type == "webdataset-audio":
        return get_audio_wds_dataset
    else:
        raise ValueError(f"Unsupported dataset type: {dataset_type}")
    

def get_data(args, preprocess_fns, epoch=0, tokenizer=None):
    preprocess_train, preprocess_val = preprocess_fns
    data = {}

    if args.datasetpath and args.datasetnames and args.datasetinfos:
        args.train_data = get_tar_path_from_dataset_name(
            args.datasetnames,
            args.datasetinfos,
            proportion=args.dataset_proportion,
            dataset_path=args.datasetpath,
            full_dataset=args.full_train_dataset,
        )
    

    if args.train_data or args.dataset_type == "synthetic":
        data["train"] = get_dataset_fn(args.train_data, args.dataset_type)(
            args, preprocess_train, is_train=True, epoch=epoch, tokenizer=tokenizer)

    if args.val_data:
        data["val"] = get_dataset_fn(args.val_data, args.dataset_type)(
            args, preprocess_val, is_train=False, tokenizer=tokenizer)

    if args.imagenet_val is not None:
        data["imagenet-val"] = get_imagenet(args, preprocess_fns, "val")

    if args.imagenet_v2 is not None:
        data["imagenet-v2"] = get_imagenet(args, preprocess_fns, "v2")

    return data
