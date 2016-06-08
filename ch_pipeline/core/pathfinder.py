"""
===============================================================
Pathfinder Telescope Model (:mod:`ch_pipeline.core.pathfinder`)
===============================================================

.. currentmodule:: ch_pipeline.core.pathfinder

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

    This class currently uses a simple Gaussian model for the primary beams.

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
    input_sel : list, optional
        Select a reduced set of feeds to use. Useful for generating small
        subsets of the data.
    """

    # Configure which feeds and layout to use
    layout = config.Property(default=None)
    correlator = config.Property(proptype=str, default=None)
    skip_non_chime = config.Property(proptype=bool, default=False)

    # Redundancy settings
    redundant = config.Property(proptype=bool, default=False)

    # Configure frequency properties
    freq_use_channels = config.Property(proptype=bool, default=True)
    channel_start = config.Property(proptype=int, default=0)
    channel_end = config.Property(proptype=int, default=1024)
    channel_bin = config.Property(proptype=int, default=1)

    # Input selection
    input_sel = config.Property(proptype=list, default=None)

    # Fix base properties
    num_cylinders = 2
    cylinder_width = 20.0
    cylinder_spacing = tools._PF_SPACE
    cylinder_length = 40.0

    rotation_angle = tools._PF_ROT

    zenith = telescope.latlon_to_sphpol([ephemeris.CHIMELATITUDE, 0.0])

    auto_correlations = True

    _pickle_keys = ['_feeds']

    #
    # === Initialisation routines ===
    #

    def __init__(self, feeds=None):
        self._feeds = feeds

    @classmethod
    def from_layout(cls, layout, correlator=None, skip=False):
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
        pf._load_layout()

        return pf

    def _load_layout(self):
        """Load the Pathfinder layout from the database.

        Generally this routine shouldn't be called directly. Use
        :method:`CHIMEPathfinder.from_layout` or configure from a YAML file.
        """
        if self.layout is None:
            raise Exception("Layout attributes not set.")

        # Fetch feed layout from database
        feeds = tools.get_correlator_inputs(self.layout, self.correlator)

        if mpiutil.size > 1:
            feeds = mpiutil.world.bcast(feeds, root=0)

        if self.skip_non_chime:
            raise Exception("Not supported.")

        self._feeds = feeds

    def _finalise_config(self):
        # Override base method to implement automatic loading of layout when
        # configuring from YAML.

        if self.layout is not None:
            print "Loading layout: %s" % str(self.layout)
            self._load_layout()

    #
    # === Redefine properties of the base class ===
    #

    # Tweak the following two properties to change the beam width
    @property
    def fwhm_e(self):
        """Full width half max of the E-plane antenna beam."""
        return 2.0 * np.pi / 3.0 * 0.7

    @property
    def fwhm_h(self):
        """Full width half max of the H-plane antenna beam."""
        return 2.0 * np.pi / 3.0 * 1.2

    # Set the approximate uv feed sizes
    @property
    def u_width(self):
        return self.cylinder_width

    # v-width property override
    @property
    def v_width(self):
        return 1.0

    def calculate_frequencies(self):
        """Override default version to give support for specifying by frequency
        channel number.
        """
        if self.freq_use_channels:
            # Calculate frequencies in terms of the channel numbers (assume the Pathfinder setup).
            basefreq = np.linspace(800.0, 400.0, 1024, endpoint=False)
            tfreq = basefreq[self.channel_start:self.channel_end]

            if len(tfreq) % self.channel_bin != 0:
                raise ValueError("Channel binning must exactly divide the total number of channels")

            self._frequencies = tfreq.reshape(-1, self.channel_bin).mean(axis=-1)
        else:
            # Otherwise use the standard method
            telescope.TransitTelescope.calculate_frequencies(self)

    @property
    def feeds(self):
        """Return a description of the feeds as a list of :class:`tools.CorrInput` instances.
        """

        if self.input_sel is None:
            feeds = self._feeds
        else:
            feeds = [ self._feeds[fi] for fi in self.input_sel ]

        return feeds

    @property
    def input_index(self):
        """An index_map describing the inputs known to the telescope. Useful
        for generating synthetic datasets.
        """
        # Extract lists of channel ID and serial numbers
        channels, feed_sn = zip(*[(feed.id, feed.input_sn) for feed in self.feeds])

        # Create an input index map and return it.
        from ch_util import andata
        return andata._generate_input_map(feed_sn, channels)

    @property
    def feedpositions(self):
        """The set of feed positions on *all* cylinders.

        This is constructed for the given layout and includes all rotations of
        the cylinder axis.

        Returns
        -------
        feedpositions : np.ndarray
            The positions in the telescope plane of the receivers. Packed as
            [[u1, v1], [u2, v2], ...].
        """

        # Fetch cylinder relative positions
        pos = tools.get_feed_positions(self.feeds, rotation=self.rotation_angle)

        # The above routine returns NaNs for non CHIME feeds. This is a bit
        # messy, so turn them into zeros.
        pos = np.nan_to_num(pos)

        return pos

    @property
    def beamclass(self):
        """Beam class definition for the Pathfinder.

        When `self.redundant` is set, the X-polarisation feeds gets `beamclass =
        0`, and the Y-polarisation gets `beamclass = 1`. If `self.redundant` is
        `False`, then the feeds are just give an increasing unique class. In
        both cases, any other type of feed gets set to `-1` and should be
        ignored.
        """
        # Make beam class just channel number.

        def _feedclass(f):
            if tools.is_chime_x(f):
                return 0
            if tools.is_chime_y(f):
                return 1
            return -1

        if self.redundant:
            return np.array([_feedclass(f) for f in self.feeds])
        else:
            beamclass = [
                fi if tools.is_chime(feed) else -1
                for fi, feed in enumerate(self.feeds)
            ]
            return np.array(beamclass)

    #
    # === Setup the primary beams ===
    #

    def beam(self, feed, freq):
        """Primary beam implementation for the CHIME Pathfinder.

        This only supports normal CHIME cylinder antennas. Asking for the beams
        for other types of inputs will cause an exception to be thrown. The
        beams from this routine are rotated by `self.rotation_angle` to account
        for the Pathfinder rotation.
        """
        # # Fetch beam parameters out of config database.

        feed_obj = self.feeds[feed]

        # Check that feed exists and is a CHIME cylinder antenna
        if feed_obj is None:
            raise ValueError("Craziness. The requested feed doesn't seem to exist.")

        if not tools.is_chime(feed_obj):
            raise ValueError('Requested feed is not a CHIME antenna.')

        # Get the beam rotation parameters.
        yaw = -self.rotation_angle
        pitch = 0.0
        roll = 0.0

        rot = np.radians([yaw, pitch, roll])

        # We can only support feeds angled parallel or perp to the cylinder
        # axis. Check for these and throw exception for anything else.
        if tools.is_chime_y(feed_obj):
            return cylbeam.beam_y(self._angpos, self.zenith,
                                  self.cylinder_width / self.wavelengths[freq],
                                  self.fwhm_e, self.fwhm_h, rot=rot)
        elif tools.is_chime_x(feed_obj):
            return cylbeam.beam_x(self._angpos, self.zenith,
                                  self.cylinder_width / self.wavelengths[freq],
                                  self.fwhm_e, self.fwhm_h, rot=rot)
        else:
            raise RuntimeError("Given polarisation (feed.pol=%s) not supported." % feed_obj.pol)

    #
    # === Override methods determining the feed pairs we should calculate ===
    #
    # These should probably get ported back into `driftscan` as options.

    def _sort_pairs(self):
        # Reimplemented sort pairs to ensure that returned array is in
        # channel order.

        # Create mask of included pairs, that are not conjugated
        tmask = np.logical_and(self._feedmask, np.logical_not(self._feedconj))
        uniq = telescope._get_indices(self._feedmap, mask=tmask)

        # Get channel id for each feed in the pair, this will be used for the sort
        ci, cj = np.array([(self.feeds[fi].id, self.feeds[fj].id) for fi, fj in uniq]).T

        # # Sort by constructing a numpy array with the keys as fields, and use
        # # np.argsort to get the indices

        # Create array of keys to sort
        dt = np.dtype('i4,i4')
        sort_arr = np.zeros(ci.size, dtype=dt)
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
        # # Reimplemented to make sure entries we always pick the upper
        # # triangle (and do not reorder to make EW baselines)
        if self.redundant:
            super(CHIMEPathfinder, self)._make_ew()

    def _unique_beams(self):
        # Override to mask out any feed where the beamclass is less than zero.
        # This is used to get exclude feeds which are not normal CHIME cylinder
        # feeds

        beam_map, beam_mask = telescope.TransitTelescope._unique_beams(self)

        # Construct a mask including only the feeds where the beam class is
        # greater than zero
        bc_mask = (self.beamclass >= 0)
        bc_mask = np.logical_and(bc_mask[:, np.newaxis], bc_mask[np.newaxis, :])

        beam_mask = np.logical_and(beam_mask, bc_mask)

        return beam_map, beam_mask