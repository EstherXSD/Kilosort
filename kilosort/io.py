from typing import Optional, Tuple, Sequence
from contextlib import contextmanager
import os, shutil
from glob import glob
from scipy.io import loadmat
import torch
import numpy as np
from torch.fft import fft, ifft, fftshift
from pathlib import Path
from kilosort import CCG
from kilosort.preprocessing import get_drift_matrix, fft_highpass

def find_binary(data_dir):
    """ find binary file in data_folder"""
    filenames  = list(data_dir.glob('*.bin'))
    if len(filenames)==0:
        raise FileNotFoundError('no binary file *.bin found in folder')
    # if there are multiple binary files, find one with "ap" tag
    if len(filenames) > 1:
        filenames = [filename for filename in filenames if 'ap' in filename.as_posix()]
    # if more than one, raise an error, user needs to specify binary
    if len(filenames) > 1:
        raise ValueError('multiple binary files in folder with "ap" tag, please specify filename')
    return filenames[0]

def load_probe(probe_path):
    """Load a .mat probe file from Kilosort2, or a PRB file and returns a dictionary
    
    adapted from https://github.com/MouseLand/pykilosort/blob/5712cfd2722a20554fa5077dd8699f68508d1b1a/pykilosort/utils.py#L592

    """
    probe = {}
    probe_path = Path(probe_path).resolve()
    required_keys = ['chanMap', 'yc', 'xc', 'n_chan']

    if probe_path.suffix == '.prb':
        # Support for PRB files.
        # !DOES NOT WORK FOR PHASE3A PROBES WITH DISCONNECTED CHANNELS!
        # Also does not remove reference channel in PHASE3B probes
        contents = probe_path.read_text()
        metadata = {}
        exec(contents, {}, metadata)
        probe['chanMap'] = []
        probe['xc'] = []
        probe['yc'] = []
        probe['kcoords'] = []
        probe['n_chan'] = 0 
        for cg in sorted(metadata['channel_groups']):
            d = metadata['channel_groups'][cg]
            ch = d['channels']
            pos = d.get('geometry', {})
            probe['chanMap'].append(ch)
            probe['n_chan'] += len(ch)
            probe['xc'].append([pos[c][0] for c in ch])
            probe['yc'].append([pos[c][1] for c in ch])
            probe['kcoords'].append([cg for c in ch])
        probe['chanMap'] = np.concatenate(probe['chanMap']).ravel().astype(np.int32)
        probe['xc'] = np.concatenate(probe['xc']).astype('float32')
        probe['yc'] = np.concatenate(probe['yc']).astype('float32')
        probe['kcoords'] = np.concatenate(probe['kcoords']).astype('float32')

    elif probe_path.suffix == '.mat':
        mat = loadmat(probe_path)
        connected = mat['connected'].ravel().astype('bool')
        probe['xc'] = mat['xcoords'].ravel().astype(np.float32)[connected]
        nc = len(probe['xc'])
        probe['yc'] = mat['ycoords'].ravel().astype(np.float32)[connected]
        probe['kcoords'] = mat.get('kcoords', np.zeros(nc)).ravel().astype(np.float32)
        probe['chanMap'] = (mat['chanMap'] - 1).ravel().astype(np.int32)[connected]  # NOTE: 0-indexing in Python
        probe['n_chan'] = (mat['chanMap'] - 1).ravel().astype(np.int32).shape[0]  # NOTE: should match the # of columns in the raw data

    for n in required_keys:
        assert n in probe.keys()

    return probe

def save_to_phy(st, clu, tF, Wall, probe, ops, results_dir=None, data_dtype=None):
    if results_dir is None:
        results_dir = ops['data_dir'].joinpath('kilosort4')
    results_dir.mkdir(exist_ok=True)

    # probe properties
    chan_map = probe['chanMap']
    channel_positions = np.stack((probe['xc'], probe['yc']), axis=-1)
    np.save((results_dir / 'channel_map.npy'), chan_map)
    np.save((results_dir / 'channel_positions.npy'), channel_positions)

    # whitening matrix ** saving real whitening matrix doesn't work with phy currently
    whitening_mat = ops['Wrot'].cpu().numpy()
    np.save((results_dir / 'whitening_mat_dat.npy'), whitening_mat)
    whitening_mat = 0.005 * np.eye(len(chan_map), dtype='float32')
    whitening_mat_inv = np.linalg.inv(whitening_mat + 1e-5 * np.eye(whitening_mat.shape[0]))
    np.save((results_dir / 'whitening_mat.npy'), whitening_mat)
    np.save((results_dir / 'whitening_mat_inv.npy'), whitening_mat_inv)

    # spike properties
    spike_times = st[:,0]
    spike_clusters = clu
    amplitudes = ((tF**2).sum(axis=(-2,-1))**0.5).cpu().numpy()
    np.save((results_dir / 'spike_times.npy'), spike_times)
    np.save((results_dir / 'spike_templates.npy'), spike_clusters)
    np.save((results_dir / 'spike_clusters.npy'), spike_clusters)
    np.save((results_dir / 'amplitudes.npy'), amplitudes)

    # template properties
    similar_templates = CCG.similarity(Wall, ops['wPCA'].contiguous(), nt=ops['nt'])
    n_temp = Wall.shape[0]
    template_amplitudes = ((Wall**2).sum(axis=(-2,-1))**0.5).cpu().numpy()
    templates = (Wall.unsqueeze(-1).cpu() * ops['wPCA'].cpu()).sum(axis=-2).numpy()
    templates = templates.transpose(0,2,1)
    templates_ind = np.tile(np.arange(Wall.shape[1])[np.newaxis, :], (templates.shape[0],1))
    np.save((results_dir / 'similar_templates.npy'), similar_templates)
    np.save((results_dir / 'templates.npy'), templates)
    np.save((results_dir / 'templates_ind.npy'), templates_ind)
    
    # contamination ratio
    is_ref, est_contam_rate = CCG.refract(clu, spike_times / ops['fs'])

    # write properties to *.tsv
    stypes = ['ContamPct', 'Amplitude', 'KSLabel']
    ks_labels = [['mua', 'good'][int(r)] for r in is_ref]
    props = [est_contam_rate*100, template_amplitudes, ks_labels]
    for stype, prop in zip(stypes, props):
        with open((results_dir / f'cluster_{stype}.tsv'), 'w') as f:
            f.write(f'cluster_id\t{stype}\n')
            for i,p in enumerate(prop):
                if stype != 'KSLabel':
                    f.write(f'{i}\t{p:.1f}\n')
                else:
                    f.write(f'{i}\t{p}\n')
        if stype == 'KSLabel':
            shutil.copyfile((results_dir / f'cluster_{stype}.tsv'), 
                            (results_dir / f'cluster_group.tsv'))

    # params.py
    dtype = "'int16'" if data_dtype is None else f"'{data_dtype}'"
    params = {'dat_path': "'" + os.fspath(ops['settings']['filename']) + "'",
            'n_channels_dat': 385,#len(chan_map),  # TODO: why is 385 hard-coded here?
            'dtype': dtype,
            'offset': 0,
            'sample_rate': ops['settings']['fs'],
            'hp_filtered': False }
    with open((results_dir / 'params.py'), 'w') as f: 
        for key in params.keys():
            f.write(f'{key} = {params[key]}\n')

    return results_dir, similar_templates, is_ref, est_contam_rate



class BinaryRWFile:
    def __init__(self, filename: str, n_chan_bin: int, fs: int = 30000, 
                 NT: int = 60000, nt: int = 61, nt0min: int = 20,
                 device: torch.device = torch.device('cpu'), write: bool = False,
                 dtype=None):
        """
        Creates/Opens a BinaryFile for reading and/or writing data that acts like numpy array

        * always assume int16 files *

        adapted from https://github.com/MouseLand/suite2p/blob/main/suite2p/io/binary.py
        
        Parameters
        ----------
        filename: str
            The filename of the file to read from or write to
        n_chan_bin: int
            number of channels
        """
        self.fs = fs
        self.n_chan_bin = n_chan_bin
        self.filename = filename
        self.NT = NT 
        self.nt = nt 
        self.nt0min = nt0min
        self.device = device
        self.n_batches = int(np.ceil(self.n_samples / self.NT))
        self.dtype = dtype if dtype is not None else 'int16'
        
        self.file = np.memmap(self.filename, mode='w+' if write else 'r',
                              dtype=self.dtype, shape=self.shape)

    @property
    def nbytesread(self):
        """number of bytes per sample (FIXED for given file)"""
        return np.int64(2 * self.n_chan_bin)

    @property
    def nbytes(self):
        """total number of bytes in the file."""
        return os.path.getsize(self.filename)
        
    @property
    def n_samples(self) -> int:
        """total number of samples in the file."""
        return int(self.nbytes // self.nbytesread)

    @property
    def shape(self) -> Tuple[int, int]:
        """
        The dimensions of the data in the file
        Returns
        -------
        n_samples: int
            number of samples
        n_chan_bin: int
            number of channels
        """
        return self.n_samples, self.n_chan_bin

    @property
    def size(self) -> int:
        """
        Returns the total number of data points

        Returns
        -------
        size: int
        """
        return np.prod(np.array(self.shape).astype(np.int64))

    def close(self) -> None:
        """
        Closes the file.
        """
        self.file._mmap.close()
        
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __setitem__(self, *items):
        sample_indices, data = items
        self.file[sample_indices] = data
        
    def __getitem__(self, *items):
        sample_indices, *crop = items
        return self.file[sample_indices]

    def padded_batch_to_torch(self, ibatch, return_inds=False):
        """ read batches from file """
        if ibatch==0:
            bstart = 0
            bend = self.NT + self.nt
        else:
            bstart = ibatch * self.NT - self.nt
            bend = min(self.n_samples, bstart + self.NT + 2*self.nt)
        data = self.file[bstart : bend]
        data = data.T
        nsamp = data.shape[-1]
        X = torch.zeros((self.n_chan_bin, self.NT + 2*self.nt), device = self.device)
        # fix the data at the edges for the first and last batch
        if ibatch==0:
            X[:, self.nt : self.nt+nsamp] = torch.from_numpy(data).to(self.device).float()
            X[:, :self.nt] = X[:, self.nt : self.nt+1]
            bstart = - self.nt
        elif ibatch==self.n_batches-1:
            X[:, :nsamp] = torch.from_numpy(data).to(self.device).float()
            X[:, nsamp:] = X[:, nsamp-1:nsamp]
            bend += self.nt
        else:
            X[:] = torch.from_numpy(data).to(self.device).float()
        inds = [bstart, bend]
        if return_inds:
            return X, inds
        else:
            return X


class BinaryFiltered(BinaryRWFile):
    def __init__(self, filename: str, n_chan_bin: int, fs: int = 30000, 
                 NT: int = 60000, nt: int = 61, nt0min: int = 20,
                 chan_map: np.ndarray = None, hp_filter: torch.Tensor = None,
                 whiten_mat: torch.Tensor = None, dshift: torch.Tensor = None,
                 device: torch.device = torch.device('cuda'), dtype=None):
        super().__init__(filename, n_chan_bin, fs, NT, nt, nt0min, device, dtype=dtype) 
        self.chan_map = chan_map
        self.whiten_mat = whiten_mat
        self.hp_filter = hp_filter
        self.dshift = dshift

    def filter(self, X, ops=None, ibatch=None):
        # pick only the channels specified in the chanMap
        if self.chan_map is not None:
            X = X[self.chan_map]

        # remove the mean of each channel, and the median across channels
        X = X - X.mean(1).unsqueeze(1)
        X = X - torch.median(X, 0)[0]
    
        # high-pass filtering in the Fourier domain (much faster than filtfilt etc)
        if self.hp_filter is not None:
            fwav = fft_highpass(self.hp_filter, NT=X.shape[1])
            X = torch.real(ifft(fft(X) * torch.conj(fwav)))
            X = fftshift(X, dim = -1)

        # whitening, with optional drift correction
        if self.whiten_mat is not None:
            if self.dshift is not None and ops is not None and ibatch is not None:
                M = get_drift_matrix(ops, self.dshift[ibatch])
                #print(M.dtype, X.dtype, self.whiten_mat.dtype)
                X = (M @ self.whiten_mat) @ X
            else:
                X = self.whiten_mat @ X
        return X

    def __getitem__(self, *items):
        sample_indices, *crop = items
        samples = self.file[sample_indices]
        X = torch.from_numpy(samples.T).to(self.device).float()
        return self.filter(X)
        
    def padded_batch_to_torch(self, ibatch, ops=None, return_inds=False):
        if return_inds:
            X, inds = super().padded_batch_to_torch(ibatch, return_inds=return_inds)
            return self.filter(X, ops, ibatch), inds
        else:
            X = super().padded_batch_to_torch(ibatch)
            return self.filter(X, ops, ibatch)


    


