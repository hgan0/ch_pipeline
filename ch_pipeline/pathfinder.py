"""
==========================================================
Pathfinder Telescope Model (:mod:`ch_pipeline.pathfinder`)
==========================================================

.. currentmodule:: ch_pipeline.pathfinder

A model for the CHIME Pathfinder telescope. This attempts to query the
configuration db (:mod:`~ch_analysis.pathfinder.configdb`) for the details of
the feeds and their positions.

Classes
=======

.. autosummary::
    :toctree: generated/

    CHIMEPathfinder
"""

import numpy as np

from caput import config, mpiutil

from drift.core import telescope
from drift.telescope import cylbeam

from ch_util import ephemeris, tools


class CHIMEPathfinder(telescope.PolarisedTelescope):
    """Model telescope for the CHIME Pathfinder.

    Attributes
    ----------
    layout : datetime or int
        Specify which layout to use.
    correlator : string
        Restrict to a specific correlator.
    skip_non_chime : boolean
        Ignore non CHIME feeds in the BeamTransfers.
    redundant : boolean
        Use only redundant baselines (default is False).
    freq_use_channels : bool, optional
        Whether to specify the frequencies in terms of channel parameters
        (default), or specify them use physical frequencies.
    channel_start : int, optional
        Which channel to start at. Default is 0.
    channel_end : int, optional
        The last channel to use. Like using a standard python range, this should
        be one larger than the last channel number you actually want. Defualt is 1024.
    channel_bin : int, optional
        Number of channels to bin together. Must exactly divide the total
        number. Default is 1.
    """

    # Configure which feeds and layout to use
    layout = config.Property(default=None)
    correlator = config.Property(proptype=str, default=None)
    skip_non_chime = config.Property(proptype=bool, default=True)

    # Redundancy settings
    redundant = config.Property(proptype=bool, default=False)

    # Configure frequency properties
    freq_use_channels = config.Property(proptype=bool, default=True)
    channel_start = config.Property(proptype=int, default=0)
    channel_end = config.Property(proptype=int, default=1024)
    channel_bin = config.Property(proptype=int, default=1)

    # Fix base properties
    num_cylinders = 2
    cylinder_width = 20.0
    cylinder_spacing = tools._PF_SPACE
    cylinder_length = 40.0

    rotation_angle = tools._PF_ROT

    zenith = telescope.latlon_to_sphpol([ephemeris.CHIMELATITUDE, 0.0])

    auto_correlations = True

    _pickle_keys = ['_feeds']

    ## Tweak the following two properties to change the beam width
    @property
    def fwhm_e(self):
        """Full width half max of the E-plane antenna beam."""
        return 2.0 * np.pi / 3.0 * 0.7

    @property
    def fwhm_h(self):
        """Full width half max of the H-plane antenna beam."""
        return 2.0 * np.pi / 3.0 * 1.2

    ## u-width property override
    @property
    def u_width(self):
        return self.cylinder_width

    ## v-width property override
    @property
    def v_width(self):
        return 1.0

    def calculate_frequencies(self):
        # Override to give support for specifying channels

        if self.freq_use_channels:
            basefreq = np.linspace(800.0, 400.0, 1024, endpoint=False)
            tfreq = basefreq[self.channel_start:self.channel_end]

            if len(tfreq) % self.channel_bin != 0:
                raise Exception("Channel binning must exactly divide the total number of channels")

            self._frequencies = tfreq.reshape(-1, self.channel_bin).mean(axis=-1)
        else:
            telescope.TransitTelescope.calculate_frequencies(self)

    @property
    def feeds(self):
        return list(self._feeds)

    @property
    def feed_index(self):
        feed_sn = [feed.input_sn for feed in self.feeds]

        # Parse a serial number into sma and slot number
        def parse_sn(sn):
            import re

            mo = re.match('(\w{6}\-\d{4})(\d{2})(\d{2})', sn)

            try:
                crate = mo.group(1)
                slot = int(mo.group(2))
                sma = int(mo.group(3))

                return crate, slot, sma
            except:
                raise RuntimeError('Serial number %s does not match expected format.' % sn)

        # Map a slot and SMA to channel id
        def get_channel(slot, sma):
            c = [None, 80, 16, 64, 0, 208, 144, 192, 128, 240, 176, 224, 160, 112, 48, 96, 32]
            channel = c[slot] + sma if slot > 0 else sma
            return channel

        channels = [get_channel(*(parse_sn(sn)[1:])) for sn in feed_sn]

        from ch_util import andata

        return andata._generate_input_map(feed_sn, channels)


    @property
    def feed_info(self):
        return self.feeds


    def __init__(self, feeds=None):

        self._feeds = feeds

    def load_layout(self):

        if self.layout is None:
            raise Exception("Layout attributes not set.")

        # Fetch feed layout from database
        feeds = tools.get_correlator_inputs(self.layout, self.correlator)

        if mpiutil.size > 1:
            feeds = mpiutil.world.bcast(feeds, root=0)

        if not self.skip_non_chime:
            raise Exception("Not supported.")

        # Filter and sort feeds
        feeds = [ feed for feed in feeds if isinstance(feed, tools.CHIMEAntenna) ]
        feeds = sorted(feeds, key=lambda feed: feed.input_sn)

        self._feeds = feeds

    @classmethod
    def from_layout(cls, layout, correlator=None, skip=True):
        """Create a Pathfinder telescope description for the specified layout.

        Parameters
        ----------
        layout : integer or datetime
            Layout id number (corresponding to one in the database), or a datetime.
        correlator : string, optional
            Name of the specific correlator. Needed to return a unique config in
            some cases.
        skip : boolean, optional
            Whether to skip non-CHIME antennas. If False, leave them in but
            set them to infinite noise (unsupported at the moment).

        Returns
        -------
        tel : CHIMEPathfinder
        """

        pf = cls()

        pf.layout = layout
        pf.correlator = correlator
        pf.skip_non_chime = skip
        pf.load_layout()

        return pf

    def _finalise_config(self):
        # Override base method to implement automatic loading of layout when
        # configuring from YAML.

        if self.layout is not None:
            print "Loading layout: %s" % str(self.layout)
            self.load_layout()

    def _sort_pairs(self):
        ## Reimplemented sort pairs to ensure that returned array is in
        ## channel order.

        # Create mask of included pairs, that are not conjugated
        tmask = np.logical_and(self._feedmask, np.logical_not(self._feedconj))
        uniq = telescope._get_indices(self._feedmap, mask=tmask)

        fi, fj = uniq[:, 0], uniq[:, 1]

        # Fetch keys by which to sort (lexicographically)
        ci = fi
        cj = fj

        ## Sort by constructing a numpy array with the keys as fields, and use
        ## np.argsort to get the indices

        # Create array of keys to sort
        dt = np.dtype('i4,i4')
        sort_arr = np.zeros(fi.size, dtype=dt)
        sort_arr['f0'] = ci
        sort_arr['f1'] = cj

        # Get map which sorts
        sort_ind = np.argsort(sort_arr)

        # Invert mapping
        tmp_sort_ind = sort_ind.copy()
        sort_ind[tmp_sort_ind] = np.arange(sort_ind.size)

        # Remap feedmap entries
        fm_copy = self._feedmap.copy()
        wmask = np.where(self._feedmask)
        fm_copy[wmask] = sort_ind[self._feedmap[wmask]]

        self._feedmap = fm_copy

    def _make_ew(self):
        ## Reimplemented to make sure entries we always pick the upper
        ## triangle (and do not reorder to make EW baselines)
        return

    @property
    def feedpositions(self):
        """The set of feed positions on *all* cylinders.

        This is constructd for the given layout and includes all rotations of
        the cylinder axis.

        Returns
        -------
        feedpositions : np.ndarray
            The positions in the telescope plane of the receivers. Packed as
            [[u1, v1], [u2, v2], ...].
        """

        # Fetch cylinder relative positions
        pos = tools.get_feed_positions(self.feeds)

        return pos  # Transpose to get into correct shape

    @property
    def channels(self):
        ## Return channel numbers. Currently the same as beamclass but might change.
        return np.array([ f.channel for f in self._feeds ])

    @property
    def beamclass(self):
        # Make beam class just channel number.

        def _feedclass(f):
            if tools.is_chime_x(f):
                return 0
            if tools.is_chime_y(f):
                return 1
            return 2

        if self.redundant:
            return np.array([_feedclass(f) for f in self.feeds])
        else:
            return np.arange(len(self.feeds))

    def beam(self, feed, freq):
        ## Fetch beam parameters out of config database.

        feed_obj = self.feeds[feed]

        # Get the beam rotation parameters.
        yaw = -self.rotation_angle
        pitch = 0.0
        roll = 0.0

        rot = np.radians([yaw, pitch, roll])

        if feed_obj is None:
            raise Exception("Craziness. The requested feed doesn't seem to exist.")

        # We can only support feeds angled parallel or perp to the cylinder
        # axis. Check for these and throw exception for anything else.
        if feed_obj.pol == "N" or feed_obj.pol == "S":
            return cylbeam.beam_y(self._angpos, self.zenith,
                                  self.cylinder_width / self.wavelengths[freq],
                                  self.fwhm_e, self.fwhm_h, rot=rot)
        elif feed_obj.pol == "E" or feed_obj.pol == "W":
            return cylbeam.beam_x(self._angpos, self.zenith,
                                  self.cylinder_width / self.wavelengths[freq],
                                  self.fwhm_e, self.fwhm_h, rot=rot)
        else:
            raise Exception("Polarisation not supported.")