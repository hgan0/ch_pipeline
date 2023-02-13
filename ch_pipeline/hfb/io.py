"""HFB tasks for reading and writing files
"""

import os
import gc
import numpy as np

from caput import pipeline
from caput import config

from ch_util import ephemeris

from draco.core import io

from beam_model.formed import FFTFormedActualBeamModel

from .containers import HFBReader


class BaseLoadFiles(io.BaseLoadFiles):
    """Base class for loading CHIME HFB data from files on disk into containers.

    Attributes
    ----------
    source_dec : float
        Declination of source in degrees.
    beam_ew_include : list
        List of East-West beam indices (i.e., in the range 0-3) to include.
        By default all four EW beams are included.
    freq_phys_range : list
        Start and stop of physical frequencies (in MHz) to read. The mean is
        used as reference frequency in evaluating beam positions (for selecting
        the beams closest to a transiting source).
    freq_phys_list : list
        List of physical frequencies (in MHz) to read. The first frequency
        in this list is also used in evaluating beam positions (for selecting
        the beams closest to a transiting source).

    Selections
    ----------
    Selections in frequency and beams can be done in two ways:
    1. By passing a `source_dec` attribute (for the beam selection) and/or
       a `freq_phys_range` or `freq_phys_list` attribute (for the frequency
       selection). If both `freq_phys_range` and `freq_phys_list` are given the
       former will take precedence, but you should clearly avoid doing this.
    2. By manually passing indices in the `selections` attribute
       (see documentation in :class:`draco.core.io.BaseLoadFiles`).
    Method 1 takes precedence over method 2. If no relevant attributes are
    passed, all frequencies/beams are read.
    """

    source_dec = config.Property(proptype=float, default=None)
    beam_ew_include = config.Property(proptype=list, default=None)
    freq_phys_list = config.Property(proptype=list, default=[])
    freq_phys_range = config.Property(proptype=list, default=[])

    def setup(self):
        """Set up frequency and beam selection."""

        # Resolve any selections provided through the `selections` attribute
        super().setup()

        # Set up frequency selection.
        cfreq = np.linspace(800.0, 400.0, 1024, endpoint=False)
        if self.freq_phys_range:
            freq_index_start = np.argmin(np.abs(cfreq - self.freq_phys_range[0]))
            freq_index_stop = np.argmin(np.abs(cfreq - self.freq_phys_range[-1]))
            self.freq_sel = slice(freq_index_start, freq_index_stop)
        elif self.freq_phys_list:
            self.freq_sel = sorted(
                set([np.argmin(np.abs(cfreq - freq)) for freq in self.freq_phys_list])
            )
        elif "freq_sel" in self._sel:
            self.freq_sel = self._sel["freq_sel"]
        else:
            self.freq_sel = slice(None)

        # Set up beam selection
        if self.source_dec:
            beam_index_ns = self._find_beam()
            self.beam_sel = slice(beam_index_ns, 1024, 256)
            if self.beam_ew_include:
                self.beam_sel = list(
                    np.arange(1024)[self.beam_sel][self.beam_ew_include]
                )
        elif "beam_sel" in self._sel:
            self.beam_sel = self._sel["beam_sel"]
        else:
            self.beam_sel = slice(None)

    def _find_beam(self):
        """Find NS beam number of beam closest to source at transit

        Returns
        -------
        beam_index_ns : int
            North-south index of beam closest to source at transit.
        """

        # Find source's telescope-y coordinate
        src_y = self.source_dec - ephemeris.CHIMELATITUDE

        # Choose beam model
        mdl = FFTFormedActualBeamModel()

        # Grid of beam numbers with EW beam number 1
        beams_ind = np.arange(1000, 1256)

        # Decide frequency (in MHz) at which to evaluate beam positions
        if self.freq_phys_range:
            freq = np.mean(self.freq_phys_range)
        elif self.freq_phys_list:
            freq = self.freq_phys_list[0]
        else:
            freq = 600.0

        # Find beam positions
        beams_xy = mdl.get_beam_positions(beams_ind, freq).squeeze()

        # Find NS beam number of beam closest to calibration source
        beam_index_ns = np.abs(beams_xy[:, 1] - src_y).argmin()

        return beam_index_ns

    def _load_filelist(self, files, time_range=(None, None)):
        """Load a list of files into the HFBData container.

        Parameters
        ----------
        files : list
            List of filenames to load into container.
        time_range: tuple
            Unix timestamps bracketing the part of the data to be loaded.
        """

        for filename in files:
            if not os.path.exists(filename):
                raise RuntimeError(f"File does not exist: {filename}")

        self.log.info(f"Loading files {files}")
        self.log.debug(f"Reading with time range: {time_range}")
        self.log.debug(f"Reading with freq selections: {self.freq_sel}")
        self.log.debug(f"Reading with beam selections: {self.beam_sel}")

        # Set up the reader
        rd = HFBReader(files)

        # Select time range
        rd.select_time_range(time_range[0], time_range[1])

        # Select frequency range
        rd.freq_sel = self.freq_sel

        # Select beams
        rd.beam_sel = self.beam_sel

        # Read files
        cont = rd.read()

        if self.redistribute is not None:
            cont.redistribute(self.redistribute)

        return cont


class LoadFilesFromParams(BaseLoadFiles):
    """Load CHIME HFB data from files given in the task's parameters.

    Attributes
    ----------
    filegroups : list or dict
        A dictionary specifying a file group, or a list of them. In addition to
        the standard components of file groups ('tag' and 'files'; see documentation
        in :class:`draco.core.io`), the file groups can also have a 'time_range',
        given as a list of two unix timestamps. Example YAML content:

    .. code-block:: yaml

        filegroups:
          - tag: '20230108'
            files: ['/mnt/gong/archive/20221221T181623Z_chime_hfb/hfb_01504956_0000.h5',
                    '/mnt/gong/archive/20221221T181623Z_chime_hfb/hfb_01510110_0000.h5']
            time_range: [1673156146.031947, 1673157946.031947]
          - tag: '20230109'
            files: ['/mnt/gong/archive/20221221T181623Z_chime_hfb/hfb_01592573_0000.h5']
            time_range: [1673242310.130873, 1673244110.130873]
    """

    filegroups = config.Property(proptype=io._list_of_filegroups)

    _fgroup_ptr = 0

    def process(self):
        """Load in each filegroup (e.g., a sidereal day).

        Returns
        -------
        ts : HFBData
            The timestream of each filegroup.
        """

        if len(self.filegroups) == self._fgroup_ptr:
            raise pipeline.PipelineStopIteration

        # Collect garbage to remove any prior data objects
        gc.collect()

        # Fetch and remove the first item in the list
        filegroup = self.filegroups[self._fgroup_ptr]
        self._fgroup_ptr += 1

        if "time_range" not in filegroup:
            filegroup["time_range"] = (None, None)

        # Read filegroup
        self.log.info(
            f"Reading filegroup {self._fgroup_ptr} of {len(self.filegroups)}."
        )
        ts = self._load_filelist(filegroup["files"], filegroup["time_range"])

        # Return timestream
        return ts


class LoadFiles(LoadFilesFromParams):
    """Load CHIME HFB data from file lists passed into the setup routine."""

    filelists = None

    def setup(self, filelists):
        """Convert lists of files to list of filegroups; set up frequency and beam selection.

        Parameters
        ----------
        filelists : list
            List of lists of filenames, or list of tuples, where each tuple
            consists of a list of filenames and a tuple of time bounds. Each
            item in the main list will be places in a single HFBData container.
        """
        if not isinstance(filelists, list):
            raise RuntimeError("Argument must be list of lists of files.")

        # Convert list of filelists to list of filegroups
        self.filegroups = []
        for i, flist in enumerate(filelists):
            tag = f"group_{i}"

            # Handle lists including time ranges
            if isinstance(flist, tuple):
                fgroup = {"files": flist[0], "time_range": flist[1], "tag": tag}
            else:
                fgroup = {"files": flist, "time_range": (None, None), "tag": tag}

            self.filegroups.append(fgroup)

        # Call the baseclass setup to resolve any selections
        super().setup()