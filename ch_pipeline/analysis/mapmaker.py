"""
========================================================
Map making tasks (:mod:`~ch_pipeline.analysis.mapmaker`)
========================================================

.. currentmodule:: ch_pipeline.analysis.mapmaker

Tools for map making from CHIME data using the m-mode formalism.

Tasks
=====

.. autosummary::
    :toctree: generated/

    RingMapMaker
"""
import numpy as np
import scipy.constants

from caput import config

from draco.core import task
from ch_util import ephemeris

from ..core import containers


class RingMapMaker(task.SingleTask):
    """A simple and quick map-maker that forms a series of beams on the meridian.

    This is designed to run on data after it has been collapsed down to
    non-redundant baselines only.

    Attributes
    ----------
    npix : int
        Number of map pixels in the el dimension.  Default is 512.

    weighting : string, one of 'uniform', 'natural', 'inverse_variance'
        How to weight the non-redundant baselines:
            'uniform' - all baselines given equal weight
            'natural' - each baseline weighted by its redundancy
            'inverse_variance' - each baselined weighted by its inverse
                                 variance according to radiometer equation

    intracyl : bool
        Include intracylinder baselines in the calculation.
        Default is True.

    abs_map : bool
        Only relevant if intracyl is False.  Take the absolute value
        of the beams instead of the real component.  Default is True.
    """

    npix = config.Property(proptype=int, default=512)

    span = config.Property(proptype=float, default=1.0)

    weighting = config.Property(proptype=str, default='natural')

    intracyl = config.Property(proptype=bool, default=True)

    def setup(self, bt):
        """Set the beamtransfer matrices to use.

        Parameters
        ----------
        bt : beamtransfer.BeamTransfer
            Beam transfer manager object. This does not need to have
            pre-generated matrices as they are not needed.
        """

        from draco.core import io
        self.tel = io.get_telescope(bt)

    def process(self, sstream):
        """Computes the ringmap.

        Parameters
        ----------
        sstream : containers.SiderealStream
            The input sidereal stream.

        Returns
        -------
        rm : containers.RingMap
        """

        from ch_util import tools

        # Redistribute over frequency
        sstream.redistribute('freq')

        nfreq = sstream.vis.local_shape[0]
        ra = getattr(sstream, 'ra', ephemeris.lsa(sstream.time))
        nra = ra.size

        # Construct mapping from vis array to unpacked 2D grid
        pol, xindex, ysep = [], [], []
        for ii, jj in sstream.prod:

            fi = self.tel.feeds[ii]
            fj = self.tel.feeds[jj]

            pol.append(2 * int(fi.pol == 'S') + int(fj.pol == 'S'))
            xindex.append(np.abs(fi.cyl - fj.cyl))
            ysep.append(fi.pos[1] - fj.pos[1])

        min_ysep, max_ysep = np.percentile([np.abs(yy) for yy in ysep if np.abs(yy) > 0.0], [0, 100])

        yindex = [int(np.round(yy / min_ysep)) for yy in ysep]

        feed_index = zip(pol, xindex, yindex)

        # Define several variables describing the baseline configuration.
        nfeed = int(np.round(max_ysep / min_ysep))
        nvis_1d = 2 * nfeed - 1
        ncyl = max(np.abs(xindex)) + 1
        nbeam = 2 * ncyl - 1

        # Define polarisation axis
        pol = np.array([x + y for x in ['X', 'Y'] for y in ['X', 'Y']])
        npol = len(pol)

        # Empty array for output
        vdr = np.zeros((nfreq, npol, nra, ncyl, nvis_1d), dtype=np.complex128)
        wgh = np.zeros((nfreq, npol, nra, ncyl, nvis_1d), dtype=np.float64)
        smp = np.zeros((nfreq, npol, nra, ncyl, nvis_1d), dtype=np.float64)

        # Unpack visibilities into new array
        for vis_ind, ind in enumerate(feed_index):

            p_ind, x_ind, y_ind = ind

            # Handle different options for weighting
            if self.weighting == 'uniform':
                w = 1.0

            elif self.weighting  == 'natural':
                w = self.tel.redundancy[vis_ind]

            elif self.weighting == 'inverse_variance':
                w = sstream.weight[:, vis_ind]

            else:
                KeyError('Do not recognize requested weighting: %s' % self.weighting)

            # Unpack visibilities
            if (x_ind == 0) and self.intracyl:
                vdr[:, p_ind, :, x_ind, y_ind] = sstream.vis[:, vis_ind]
                vdr[:, p_ind, :, x_ind, -y_ind] = sstream.vis[:, vis_ind].conj()

                wgh[:, p_ind, :, x_ind, y_ind] = sstream.weight[:, vis_ind]
                wgh[:, p_ind, :, x_ind, -y_ind] = sstream.weight[:, vis_ind]

                smp[:, p_ind, :, x_ind, y_ind] = w
                smp[:, p_ind, :, x_ind, -y_ind] = w

            else:
                vdr[:, p_ind, :, x_ind, y_ind] = sstream.vis[:, vis_ind]

                wgh[:, p_ind, :, x_ind, y_ind] = sstream.weight[:, vis_ind]

                smp[:, p_ind, :, x_ind, y_ind] = w

        # Remove auto-correlations
        if self.intracyl:
            smp[..., 0, 0] = 0.0

        # Normalize the weighting function
        coeff = np.full(ncyl, 2.0, dtype=np.float)
        coeff[0] -= self.intracyl

        smp *= tools.invert_no_zero(np.sum(np.dot(coeff, smp), axis=-1))[..., np.newaxis, np.newaxis]

        # Construct phase array
        el = self.span * np.linspace(-1.0, 1.0, self.npix)

        vis_pos_1d = np.fft.fftfreq(nvis_1d, d=(1.0 / (nvis_1d * min_ysep)))

        # Create empty ring map
        rm = containers.RingMap(beam=nbeam, el=el, pol=pol, ra=ra,
                                axes_from=sstream, attrs_from=sstream)
        rm.redistribute('freq')

        # Add datasets
        rm.add_dataset('rms')
        rm.add_dataset('dirty_beam')

        # Estimate RMS thermal noise in ring map
        rm.rms[:] = np.sqrt(np.sum(np.dot(coeff, tools.invert_no_zero(wgh) * smp**2.0), axis=-1))

        # Loop over local frequencies and fill ring map
        for lfi, fi in sstream.vis[:].enumerate(0):

            # Get the current freq
            fr = sstream.freq[fi]

            wv = scipy.constants.c * 1e-6 / fr

            # Create array that will be used for the inverse
            # discrete Fourier transform in el direction
            pa = np.exp(2.0J * np.pi * vis_pos_1d[:, np.newaxis] * el[np.newaxis, :] / wv)

            bfm = np.fft.irfft(np.dot(smp[lfi] * vdr[lfi], pa), nbeam, axis=2) * nbeam
            sb = np.fft.irfft(np.dot(smp[lfi], pa), nbeam, axis=2) * nbeam

            # Save to container
            rm.map[fi] = bfm
            rm.dirty_beam[fi] = sb

        return rm
