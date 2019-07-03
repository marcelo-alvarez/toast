# Copyright (c) 2015-2019 by the parties listed in the AUTHORS file.
# All rights reserved.  Use of this source code is governed by
# a BSD-style license that can be found in the LICENSE file.

from .mpi import MPITestCase

import os
import shutil

import numpy as np
import numpy.testing as nt

from ..tod import (
    TODGround,
    OpPointingHpix,
    AnalyticNoise,
    OpSimGradient,
    OpSimNoise,
    OpSimScan,
    OpSimAtmosphere,
    atm_available,
    atm_available_utils,
    atm_available_mpi,
)

from ._helpers import create_outdir, create_distdata, boresight_focalplane


class OpsSimAtmosphereTest(MPITestCase):
    def setUp(self):
        fixture_name = os.path.splitext(os.path.basename(__file__))[0]
        self.outdir = create_outdir(self.comm, fixture_name)

        # Create one observation per group, and each observation will have
        # one detector per process and a single chunk.

        self.data = create_distdata(self.comm, obs_per_group=1)

        # This serial data will exist separately on each process
        self.data_serial = create_distdata(None, obs_per_group=1)

        self.ndet = self.data.comm.group_size
        self.rate = 20.0

        # Create detectors with white noise
        self.NET = 5.0

        dnames, dquat, depsilon, drate, dnet, dfmin, dfknee, dalpha = boresight_focalplane(
            self.ndet, samplerate=self.rate, fknee=0.0, net=self.NET
        )

        dnames_serial, dquat_serial, _, _, _, _, _, _ = boresight_focalplane(
            1, samplerate=self.rate, fknee=0.0, net=self.NET
        )

        # Samples per observation
        self.totsamp = 100000

        # Pixelization
        nside = 256
        self.sim_nside = nside
        self.map_nside = nside

        # Scan properties
        self.site_lon = "-67:47:10"
        self.site_lat = "-22:57:30"
        self.site_alt = 5200.0
        self.coord = "C"
        self.azmin = 45
        self.azmax = 55
        self.el = 60
        self.scanrate = 1.0
        self.scan_accel = 0.1
        self.CES_start = None

        # Populate the single observation per group

        tod = TODGround(
            self.data.comm.comm_group,
            dquat,
            self.totsamp,
            detranks=self.data.comm.group_size,
            firsttime=0.0,
            rate=self.rate,
            site_lon=self.site_lon,
            site_lat=self.site_lat,
            site_alt=self.site_alt,
            azmin=self.azmin,
            azmax=self.azmax,
            el=self.el,
            coord=self.coord,
            scanrate=self.scanrate,
            scan_accel=self.scan_accel,
            CES_start=self.CES_start,
        )

        tod_serial = TODGround(
            self.data_serial.comm.comm_group,
            dquat_serial,
            self.totsamp,
            detranks=self.data_serial.comm.group_size,
            firsttime=0.0,
            rate=self.rate,
            site_lon=self.site_lon,
            site_lat=self.site_lat,
            site_alt=self.site_alt,
            azmin=self.azmin,
            azmax=self.azmax,
            el=self.el,
            coord=self.coord,
            scanrate=self.scanrate,
            scan_accel=self.scan_accel,
            CES_start=self.CES_start,
        )

        self.common_flag_mask = tod.TURNAROUND

        common_flags = tod.read_common_flags()

        # Number of flagged samples in each observation.  Only the first row
        # of the process grid needs to contribute, since all process columns
        # have identical common flags.
        nflagged = 0
        if (tod.grid_comm_col is None) or (tod.grid_comm_col.rank == 0):
            nflagged += np.sum((common_flags & self.common_flag_mask) != 0)

        # Number of flagged samples across all observations
        self.nflagged = None
        if self.comm is None:
            self.nflagged = nflagged
        else:
            self.nflagged = self.data.comm.comm_world.allreduce(nflagged)

        self.data.obs[0]["tod"] = tod
        self.data_serial.obs[0]["tod"] = tod_serial
        return

    def test_atm(self):
        rank = 0
        do_serial = False
        if self.comm is not None:
            rank = self.comm.rank
            do_serial = True

        freq = None
        cachedir = self.outdir

        # Generate an atmosphere sim with no loading or absorption.
        atm = OpSimAtmosphere(
            out="atm",
            realization=0,
            component=123456,
            lmin_center=0.01,
            lmin_sigma=0.001,
            lmax_center=10,
            lmax_sigma=10,
            zatm=40000.0,
            zmax=2000.0,
            xstep=100.0,
            ystep=100.0,
            zstep=100.0,
            nelem_sim_max=10000,
            verbosity=0,
            gain=1,
            z0_center=2000,
            z0_sigma=0,
            apply_flags=True,
            common_flag_name=None,
            common_flag_mask=self.common_flag_mask,
            flag_name=None,
            flag_mask=255,
            report_timing=True,
            wind_dist=10000,
            cachedir=cachedir,
            flush=False,
            freq=None,
        )

        atm_utils = None
        if atm_available_utils:
            freq = 150.0
            atm_utils = OpSimAtmosphere(
                out="atm-utils",
                realization=0,
                component=123456,
                lmin_center=0.01,
                lmin_sigma=0.001,
                lmax_center=10,
                lmax_sigma=10,
                zatm=40000.0,
                zmax=2000.0,
                xstep=100.0,
                ystep=100.0,
                zstep=100.0,
                nelem_sim_max=10000,
                verbosity=0,
                gain=1,
                z0_center=2000,
                z0_sigma=0,
                apply_flags=True,
                common_flag_name=None,
                common_flag_mask=self.common_flag_mask,
                flag_name=None,
                flag_mask=255,
                report_timing=True,
                wind_dist=10000,
                cachedir=cachedir,
                flush=False,
                freq=freq,
            )

        # Do the simulation with the default data distribution and communicator

        atm.exec(self.data)
        if atm_utils is not None:
            atm_utils.exec(self.data)

        # Now do an explicit serial calculation on each process for one detector.

        atm.exec(self.data_serial)
        if atm_utils is not None:
            atm_utils.exec(self.data_serial)

        # Check that the two cases agree on the process which has overlap between them
        tod = self.data.obs[0]["tod"]
        oid = self.data.obs[0]["id"]
        tod_serial = self.data_serial.obs[0]["tod"]
        oid_serial = self.data_serial.obs[0]["id"]
        if oid == oid_serial:
            for d in tod.local_dets:
                if d in tod_serial.local_dets:
                    cname = "atm_{}".format(d)
                    ref = tod.cache.reference(cname)
                    ref_serial = tod_serial.cache.reference(cname)
                    nt.assert_almost_equal(ref[:], ref_serial[:])
                    if atm_utils is not None:
                        cname = "atm-utils_{}".format(d)
                        ref = tod.cache.reference(cname)
                        ref_serial = tod_serial.cache.reference(cname)
                        nt.assert_almost_equal(ref[:], ref_serial[:])

        return