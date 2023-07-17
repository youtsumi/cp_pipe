#!/usr/bin/env python

#
# LSST Data Management System
#
# Copyright 2008-2017  AURA/LSST.
#
# This product includes software developed by the
# LSST Project (http://www.lsst.org/).
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the LSST License Statement and
# the GNU General Public License along with this program.  If not,
# see <https://www.lsstcorp.org/LegalNotices/>.
#
"""Test cases for cp_pipe linearity code."""

import unittest
import numpy as np

import lsst.utils
import lsst.utils.tests

from lsst.ip.isr import PhotonTransferCurveDataset, PhotodiodeCalib

import lsst.afw.image
import lsst.afw.math
from lsst.cp.pipe import LinearitySolveTask
from lsst.cp.pipe.ptc import PhotonTransferCurveSolveTask
from lsst.cp.pipe.utils import funcPolynomial
from lsst.ip.isr.isrMock import FlatMock, IsrMock


class FakeCamera(list):
    def getName(self):
        return "FakeCam"


class LinearityTaskTestCase(lsst.utils.tests.TestCase):
    """Test case for the linearity tasks."""

    def setUp(self):
        mock_image_config = IsrMock.ConfigClass()
        mock_image_config.flatDrop = 0.99999
        mock_image_config.isTrimmed = True

        self.dummy_exposure = FlatMock(config=mock_image_config).run()
        self.detector = self.dummy_exposure.getDetector()
        self.input_dims = {"detector": 0}

        self.camera = FakeCamera([self.detector])

        self.amp_names = []
        for amp in self.detector:
            self.amp_names.append(amp.getName())

    def _create_ptc(self, amp_names, exp_times, means, ccobcurr=None):
        """
        Create a PTC with values for linearity tests.

        Parameters
        ----------
        amp_names : `list` [`str`]
            Names of amps.
        exp_times : `np.ndarray`
            Array of exposure times.
        means : `np.ndarray`
            Array of means.
        ccobcurr : `np.ndarray`, optional
            Array of CCOBCURR to put into auxiliary values.

        Returns
        -------
        ptc : `lsst.ip.isr.PhotonTransferCurveDataset`
            PTC filled with relevant values.
        """
        exp_id_pairs = np.arange(len(exp_times)*2).reshape((len(exp_times), 2)).tolist()

        datasets = []
        for i in range(len(exp_times)):
            partial = PhotonTransferCurveDataset(amp_names, ptcFitType="PARTIAL", covMatrixSide=1)
            for amp_name in amp_names:
                # For the first amp, we add a few bad points.
                if amp_name == amp_names[0] and i >= 5 and i < 7:
                    exp_id_mask = False
                else:
                    exp_id_mask = True

                partial.setAmpValuesPartialDataset(
                    amp_name,
                    inputExpIdPair=exp_id_pairs[i],
                    rawExpTime=exp_times[i],
                    rawMean=means[i],
                    rawVar=1.0,
                    kspValue=1.0,
                    expIdMask=exp_id_mask,
                )

            if ccobcurr is not None:
                partial.setAuxValuesPartialDataset({"CCOBCURR": ccobcurr[i]})

            datasets.append(partial)

            datasets.append(PhotonTransferCurveDataset(amp_names, ptcFitType="DUMMY"))

        config = PhotonTransferCurveSolveTask.ConfigClass()
        config.maximumRangeCovariancesAstier = 1
        solve_task = PhotonTransferCurveSolveTask(config=config)
        ptc = solve_task.run(datasets).outputPtcDataset

        # Make the last amp a bad amp.
        ptc.badAmps = [amp_names[-1]]

        return ptc

    def _check_linearity(self, linearity_type, min_adu=0.0, max_adu=100000.0):
        """Run and check linearity.

        Parameters
        ----------
        linearity_type : `str`
            Must be ``Polynomial``, ``Squared``, or ``LookupTable``.
        min_adu : `float`, optional
            Minimum cut on ADU for fit.
        max_adu : `float`, optional
            Maximum cut on ADU for fit.
        """
        flux = 1000.
        time_vec = np.arange(1., 101., 5)
        k2_non_linearity = -5e-6
        coeff = k2_non_linearity/(flux**2.)

        mu_vec = flux * time_vec + k2_non_linearity * time_vec**2.

        ptc = self._create_ptc(self.amp_names, time_vec, mu_vec)

        config = LinearitySolveTask.ConfigClass()
        config.linearityType = linearity_type
        config.minLinearAdu = min_adu
        config.maxLinearAdu = max_adu

        task = LinearitySolveTask(config=config)
        linearizer = task.run(ptc, [self.dummy_exposure], self.camera, self.input_dims).outputLinearizer

        if linearity_type == "LookupTable":
            t_max = config.maxLookupTableAdu / flux
            time_range = np.linspace(0.0, t_max, config.maxLookupTableAdu)
            signal_ideal = time_range * flux
            signal_uncorrected = funcPolynomial(np.array([0.0, flux, k2_non_linearity]), time_range)
            linearizer_table_row = signal_ideal - signal_uncorrected

        # Skip the last amp which is marked bad.
        for i, amp_name in enumerate(ptc.ampNames[:-1]):
            if linearity_type in ["Squared", "Polynomial"]:
                self.assertFloatsAlmostEqual(linearizer.fitParams[amp_name][0], 0.0, atol=1e-2)
                self.assertFloatsAlmostEqual(linearizer.fitParams[amp_name][1], 1.0, rtol=1e-5)
                self.assertFloatsAlmostEqual(linearizer.fitParams[amp_name][2], coeff, rtol=1e-6)

                if linearity_type == "Polynomial":
                    self.assertFloatsAlmostEqual(linearizer.fitParams[amp_name][3], 0.0)

                if linearity_type == "Squared":
                    self.assertEqual(len(linearizer.linearityCoeffs[amp_name]), 1)
                    self.assertFloatsAlmostEqual(linearizer.linearityCoeffs[amp_name][0], -coeff, rtol=1e-6)
                else:
                    self.assertEqual(len(linearizer.linearityCoeffs[amp_name]), 2)
                    self.assertFloatsAlmostEqual(linearizer.linearityCoeffs[amp_name][0], -coeff, rtol=1e-6)
                    self.assertFloatsAlmostEqual(linearizer.linearityCoeffs[amp_name][1], 0.0)

            else:
                index = linearizer.linearityCoeffs[amp_name][0]
                self.assertEqual(index, i)
                self.assertEqual(len(linearizer.tableData[index, :]), len(linearizer_table_row))
                self.assertFloatsAlmostEqual(linearizer.tableData[index, :], linearizer_table_row, rtol=1e-4)

            lin_mask = np.isfinite(linearizer.fitResiduals[amp_name])
            lin_mask_expected = (mu_vec > min_adu) & (mu_vec < max_adu) & ptc.expIdMask[amp_name]

            self.assertListEqual(lin_mask.tolist(), lin_mask_expected.tolist())
            self.assertFloatsAlmostEqual(linearizer.fitResiduals[amp_name][lin_mask], 0.0, atol=1e-2)

            # If we apply the linearity correction, we should get the true
            # linear values out.
            image = lsst.afw.image.ImageF(len(mu_vec), 1)
            image.array[:, :] = mu_vec
            lin_func = linearizer.getLinearityTypeByName(linearizer.linearityType[amp_name])
            lin_func()(
                image,
                coeffs=linearizer.linearityCoeffs[amp_name],
                table=linearizer.tableData,
                log=None,
            )

            linear_signal = flux * time_vec
            self.assertFloatsAlmostEqual(image.array[0, :] / linear_signal, 1.0, rtol=1e-6)

    def test_linearity_polynomial(self):
        """Test linearity with polynomial fit."""
        self._check_linearity("Polynomial")

    def test_linearity_squared(self):
        """Test linearity with a single order squared solution."""
        self._check_linearity("Squared")

    def test_linearity_table(self):
        """Test linearity with a lookup table solution."""
        self._check_linearity("LookupTable")

    def test_linearity_polynomial_aducuts(self):
        """Test linearity with polynomial and ADU cuts."""
        self._check_linearity("Polynomial", min_adu=10000.0, max_adu=90000.0)

    def _check_linearity_spline(self, do_pd_offsets=False):
        """Check linearity with a spline solution.

        Parameters
        ----------
        do_pd_offsets : `bool`, optional
            Apply offsets to the photodiode data.
        """
        np.random.seed(12345)

        # Create a test dataset representative of real data.
        pd_values = np.linspace(1e-8, 2e-5, 200)
        time_values = pd_values * 1000000.
        linear_ratio = 5e9
        mu_linear = linear_ratio * pd_values

        # Test spline parameters are taken from a test fit to LSSTCam
        # data, run 7193D, detector 22, amp C00. The exact fit is not
        # important, but this is only meant to be representative of
        # the shape of the non-linearity that we see.

        n_nodes = 10

        non_lin_spline_nodes = np.linspace(0, mu_linear.max(), n_nodes)
        non_lin_spline_values = np.array(
            [0.0, -8.87, 1.46, 1.69, -6.92, -68.23, -78.01, -11.56, 80.26, 185.01]
        )

        spl = lsst.afw.math.makeInterpolate(
            non_lin_spline_nodes,
            non_lin_spline_values,
            lsst.afw.math.stringToInterpStyle("AKIMA_SPLINE"),
        )

        mu_values = mu_linear + spl.interpolate(mu_linear)
        mu_values += np.random.normal(scale=mu_values, size=len(mu_values)) / 10000.

        # Add some outlier values.
        outlier_indices = np.arange(5) + 170
        mu_values[outlier_indices] += 200.0

        # Add some small offsets to the pd_values if requested.
        pd_values_offset = pd_values.copy()
        ccobcurr = None
        if do_pd_offsets:
            ccobcurr = np.zeros(pd_values.size)
            group0 = np.arange(50)
            group1 = np.arange(50) + 50
            group2 = np.arange(50) + 100
            group3 = np.arange(50) + 150
            ccobcurr[group0] = 0.01
            ccobcurr[group1] = 0.02
            ccobcurr[group2] = 0.03
            ccobcurr[group3] = 0.04

            pd_offset_factors = [0.995, 1.0, 1.005, 1.002]
            pd_values_offset[group0] *= pd_offset_factors[0]
            pd_values_offset[group2] *= pd_offset_factors[2]
            pd_values_offset[group3] *= pd_offset_factors[3]

        ptc = self._create_ptc(self.amp_names, time_values, mu_values, ccobcurr=ccobcurr)

        # And create a bunch of PD datasets.
        amp_name = ptc.ampNames[0]
        exp_id_pairs = ptc.inputExpIdPairs[amp_name]

        pd_handles = []

        for i, exp_id_pair in enumerate(exp_id_pairs):
            time_samples = np.linspace(0, 20.0, 100)
            current_samples = np.zeros(100)
            current_samples[50] = -1.0*pd_values_offset[i]

            pd_calib = PhotodiodeCalib(timeSamples=time_samples, currentSamples=current_samples)
            pd_calib.currentScale = -1.0
            pd_calib.integrationMethod = "CHARGE_SUM"

            pd_handles.append(
                lsst.pipe.base.InMemoryDatasetHandle(
                    pd_calib,
                    dataId={"exposure": exp_id_pair[0]},
                )
            )
            pd_handles.append(
                lsst.pipe.base.InMemoryDatasetHandle(
                    pd_calib,
                    dataId={"exposure": exp_id_pair[1]},
                )
            )

        config = LinearitySolveTask.ConfigClass()
        config.linearityType = "Spline"
        config.usePhotodiode = True
        config.photodiodeIntegrationMethod = "CHARGE_SUM"
        config.minLinearAdu = 0.0
        config.maxLinearAdu = np.max(mu_values) + 1.0
        config.splineKnots = n_nodes

        if do_pd_offsets:
            config.splineGroupingColumn = "CCOBCURR"

        task = LinearitySolveTask(config=config)
        linearizer = task.run(
            ptc,
            [self.dummy_exposure],
            self.camera,
            self.input_dims,
            inputPhotodiodeData=pd_handles,
        ).outputLinearizer

        # Skip the last amp which is marked bad.
        for amp_name in ptc.ampNames[:-1]:
            lin_mask = np.isfinite(linearizer.fitResiduals[amp_name])

            # Make sure that anything in the input mask is still masked.
            check, = np.where(~ptc.expIdMask[amp_name])
            if len(check) > 0:
                self.assertEqual(np.all(lin_mask[check]), False)

            # Make sure the outliers are masked.
            self.assertEqual(np.all(lin_mask[outlier_indices]), False)

            # The first point at very low flux is noisier and so we exclude
            # it from the test here.
            self.assertFloatsAlmostEqual(
                (linearizer.fitResiduals[amp_name][lin_mask] / mu_linear[lin_mask])[1:],
                0.0,
                atol=1e-3,
            )

            # If we apply the linearity correction, we should get the true
            # linear values out.
            image = lsst.afw.image.ImageF(len(mu_values), 1)
            image.array[:, :] = mu_values
            lin_func = linearizer.getLinearityTypeByName(linearizer.linearityType[amp_name])
            lin_func()(
                image,
                coeffs=linearizer.linearityCoeffs[amp_name],
                log=None,
            )

            # We scale by the median because of ambiguity in the overall
            # gain parameter which is not part of the non-linearity.
            ratio = image.array[0, lin_mask]/mu_linear[lin_mask]
            self.assertFloatsAlmostEqual(
                ratio / np.median(ratio),
                1.0,
                rtol=5e-4,
            )

            # Check that the spline parameters recovered are consistent,
            # with input to some low-grade precision.
            # The first element should be identically zero.
            self.assertFloatsEqual(linearizer.linearityCoeffs[amp_name][0], 0.0)

            # We have two different comparisons here; for the terms that are
            # |value| < 20 (offset) or |value| > 20 (ratio), to avoid
            # divide-by-small-number problems. In all cases these are
            # approximate, and the real test is in the residuals.
            small = (np.abs(non_lin_spline_values) < 20)

            spline_atol = 5.0 if do_pd_offsets else 2.0
            spline_rtol = 0.1 if do_pd_offsets else 0.05

            self.assertFloatsAlmostEqual(
                linearizer.linearityCoeffs[amp_name][n_nodes:][small],
                non_lin_spline_values[small],
                atol=spline_atol,
            )
            self.assertFloatsAlmostEqual(
                linearizer.linearityCoeffs[amp_name][n_nodes:][~small],
                non_lin_spline_values[~small],
                rtol=spline_rtol,
            )

            # And check the offsets if they were included.
            if do_pd_offsets:
                # The relative scaling is to group 1.
                fit_offset_factors = linearizer.fitParams[amp_name][1] / linearizer.fitParams[amp_name]

                self.assertFloatsAlmostEqual(fit_offset_factors, np.array(pd_offset_factors), rtol=6e-4)

    def test_linearity_spline(self):
        self._check_linearity_spline()

    def test_linearity_spline_offsets(self):
        self._check_linearity_spline(do_pd_offsets=True)


class TestMemory(lsst.utils.tests.MemoryTestCase):
    pass


def setup_module(module):
    lsst.utils.tests.init()


if __name__ == "__main__":
    lsst.utils.tests.init()
    unittest.main()
