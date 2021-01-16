# Copyright (c) 2015-2020 by the parties listed in the AUTHORS file.
# All rights reserved.  Use of this source code is governed by
# a BSD-style license that can be found in the LICENSE file.

import os

import numpy as np
import numpy.testing as nt

from astropy import units as u

import healpy as hp

from .mpi import MPITestCase

from ..noise import Noise

from ..vis import set_matplotlib_backend

from .. import ops as ops

from ..templates import Offset, AmplitudesMap

from ..ops.mapmaker_solve import SolverRHS, SolverLHS

from ._helpers import create_outdir, create_satellite_data


class MapmakerSolveTest(MPITestCase):
    def setUp(self):
        fixture_name = os.path.splitext(os.path.basename(__file__))[0]
        self.outdir = create_outdir(self.comm, fixture_name)
        np.random.seed(123456)

    def test_rhs(self):
        # Create a fake satellite data set for testing
        data = create_satellite_data(self.comm)

        # Create an uncorrelated noise model from focalplane detector properties
        default_model = ops.DefaultNoiseModel(noise_model="noise_model")
        default_model.apply(data)

        # Simulate noise
        sim_noise = ops.SimNoise(noise_model=default_model.noise_model, out="noise")
        sim_noise.apply(data)

        # Pointing operator
        pointing = ops.PointingHealpix(nside=64, mode="IQU", hwp_angle="hwp_angle")

        # Build the covariance and hits
        cov_and_hits = ops.CovarianceAndHits(
            pixel_dist="pixel_dist",
            pointing=pointing,
            noise_model=default_model.noise_model,
        )
        cov_and_hits.apply(data)

        # Set up binner
        binner = ops.BinMap(
            pixel_dist="pixel_dist",
            covariance=cov_and_hits.covariance,
            det_data=sim_noise.out,
            pointing=pointing,
            noise_model=default_model.noise_model,
            save_pointing=False,
        )

        # Set up template matrix with just an offset template.

        # Use 1/10 of an observation as the baseline length.  Make it not evenly
        # divisible in order to test handling of the final amplitude.
        ob_time = data.obs[0].shared["times"][-1] - data.obs[0].shared["times"][0]
        step_seconds = float(int(ob_time / 10.0))
        tmpl = Offset(
            times="times",
            noise_model=default_model.noise_model,
            step_time=step_seconds,
        )
        tmatrix = ops.TemplateMatrix(templates=[tmpl])
        tmatrix.amplitudes = "RHS"

        # Set up RHS operator and run it.  We set overwrite=False so that a temporary
        # detdata object is used.  We need the original timestream for our manual check
        # below.

        rhs_calc = SolverRHS(
            det_data=sim_noise.out,
            overwrite=False,
            binning=binner,
            template_matrix=tmatrix,
        )
        rhs_calc.apply(data)

        # Get the output binned map used by the RHS operator.
        rhs_binned = data[binner.binned]

        bd = data[binner.binned].data
        print("rhs binned map = ", bd[bd != 0])

        # Manual check.  This applies the same operators as the RHS operator, but
        # checks things along the way.  And these lower-level operators are unit
        # tested elsewhere as well...

        # Make the binned map in a different location
        binner.binned = "check"
        binner.det_data = sim_noise.out
        binner.apply(data)

        check_binned = data[binner.binned]
        bd = data[binner.binned].data
        print("check binned map = ", bd[bd != 0], flush=True)

        # Verify that the binned map elements agree
        np.testing.assert_equal(rhs_binned.raw.array(), check_binned.raw.array())

        # Scan the binned map and subtract from the original detector data.
        pointing.apply(data)

        scan_map = ops.ScanMap(
            pixels=pointing.pixels,
            weights=pointing.weights,
            map_key=binner.binned,
            det_data=sim_noise.out,
            subtract=True,
        )
        scan_map.apply(data)

        # Apply diagonal noise weight.
        nw = ops.NoiseWeight(noise_model=binner.noise_model, det_data=sim_noise.out)
        nw.apply(data)

        # Project our timestreams to template amplitudes.  Store this in a different
        # data key that the RHS operator.

        tmatrix.amplitudes = "check_RHS"
        tmatrix.det_data = sim_noise.out
        tmatrix.apply(data)

        # Verify that the output amplitudes agree
        np.testing.assert_equal(
            data["RHS"][tmpl.name].local, data["check_RHS"][tmpl.name].local
        )

        del data
        return

    def test_lhs(self):
        # Create a fake satellite data set for testing
        data = create_satellite_data(self.comm)

        # Create an uncorrelated noise model from focalplane detector properties
        default_model = ops.DefaultNoiseModel(noise_model="noise_model")
        default_model.apply(data)

        # Set up template matrix with just an offset template.

        # Use 1/10 of an observation as the baseline length.  Make it not evenly
        # divisible in order to test handling of the final amplitude.
        ob_time = data.obs[0].shared["times"][-1] - data.obs[0].shared["times"][0]
        step_seconds = float(int(ob_time / 10.0))
        tmpl = Offset(
            times="times",
            noise_model=default_model.noise_model,
            step_time=step_seconds,
        )
        tmatrix = ops.TemplateMatrix(templates=[tmpl])

        # For testing the LHS calculation, we first generate fake template
        # amplitudes.  Then we manually check the result by projecting these to
        # a timestream and running the RHS operator on it.  In the case of no noise
        # prior, this should be equivalent.  We use a temperature-only pointing
        # matrix so that it can be consistent with constant-valued timestreams.

        # Manually set the data for this template (normally done by
        # TemplateMatrix.exec()) so we can pre-generate amplitudes.
        tmpl.data = data
        data["amplitudes"] = AmplitudesMap()
        data["amplitudes"][tmpl.name] = tmpl.zeros()
        data["amplitudes"][tmpl.name].local[:] = np.random.uniform(
            low=-1000.0, high=1000.0, size=data["amplitudes"][tmpl.name].n_local
        )

        print("amplitudes = ", data["amplitudes"])

        for ob in data.obs:
            ob.detdata.create("signal")

        tmatrix.amplitudes = "amplitudes"
        tmatrix.det_data = "signal"
        tmatrix.data = data
        tmatrix.transpose = False
        tmatrix.apply(data)

        for ob in data.obs:
            print("signal = ", ob.detdata["signal"])

        # Pointing operator
        pointing = ops.PointingHealpix(nside=64, mode="I", hwp_angle="hwp_angle")

        # Build the covariance and hits
        cov_and_hits = ops.CovarianceAndHits(
            pixel_dist="pixel_dist",
            pointing=pointing,
            noise_model=default_model.noise_model,
        )
        cov_and_hits.apply(data)

        # Set up binner
        binner = ops.BinMap(
            pixel_dist="pixel_dist",
            covariance=cov_and_hits.covariance,
            det_data="signal",
            pointing=pointing,
            noise_model=default_model.noise_model,
            save_pointing=False,
        )

        # Set up RHS operator and run it.

        tmatrix.amplitudes = "amplitudes_check"
        binner.binned = "rhs_binned"
        rhs_calc = SolverRHS(
            det_data="signal",
            overwrite=True,
            binning=binner,
            template_matrix=tmatrix,
        )
        rhs_calc.apply(data)

        bd = data[binner.binned].data
        print("rhs binned map = ", bd[bd != 0])

        print("amplitudes_check = ", data["amplitudes_check"])

        # Now we will run the LHS operator and compare.  Re-use the previous detdata
        # array for temp space.

        tmatrix.amplitudes = "amplitudes"
        binner.binned = "lhs_binned"
        out_amps = "out_amplitudes"
        lhs_calc = SolverLHS(
            det_temp="signal",
            binning=binner,
            template_matrix=tmatrix,
            out=out_amps,
        )
        lhs_calc.apply(data)

        print("amplitudes out = ", data[out_amps])

        # Verify that the output amplitudes agree
        np.testing.assert_equal(
            data[out_amps][tmpl.name].local,
            data["amplitudes_check"][tmpl.name].local,
        )

        del data
        return
