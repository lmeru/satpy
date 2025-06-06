#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2018 Satpy developers
#
# This file is part of satpy.
#
# satpy is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# satpy is distributed in the hope that it will be useful, but WITHOUT ANY
# WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR
# A PARTICULAR PURPOSE.  See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# satpy.  If not, see <http://www.gnu.org/licenses/>.

"""The ahi_hsd reader tests package."""

from __future__ import annotations

import contextlib
import datetime as dt
import unittest
import warnings
from typing import Any, Dict
from unittest import mock

import dask.array as da
import numpy as np
import pytest

from satpy.readers.ahi_hsd import AHIHSDFileHandler, _NominalTimeCalculator
from satpy.readers.core.utils import get_geostationary_mask
from satpy.tests.utils import make_dataid

InfoDict = Dict[str, Any]

FAKE_BASIC_INFO: InfoDict = {
    "blocklength": 0,
    "satellite": "Himawari-8",
    "observation_area": "FLDK",
    "observation_start_time": 58413.12523839,
    "observation_end_time": 58413.132182834444,
    "observation_timeline": "0300",
}
FAKE_DATA_INFO: InfoDict = {
    "blocklength": 50,
    "compression_flag_for_data": 0,
    "hblock_number": 2,
    "number_of_bits_per_pixel": 16,
    "number_of_columns": np.array([11000]),
    "number_of_lines": np.array([1100]),
    "spare": "",
}
FAKE_PROJ_INFO: InfoDict = {
    "CFAC": 40932549,
    "COFF": 5500.5,
    "LFAC": 40932549,
    "LOFF": 5500.5,
    "blocklength": 127,
    "coeff_for_sd": 1737122264.0,
    "distance_from_earth_center": 42164.0,
    "earth_equatorial_radius": 6378.137,
    "earth_polar_radius": 6356.7523,
    "hblock_number": 3,
    "req2_rpol2": 1.006739501,
    "req2_rpol2_req2": 0.0066943844,
    "resampling_size": 4,
    "resampling_types": 0,
    "rpol2_req2": 0.993305616,
    "spare": "",
    "sub_lon": 140.7,
}
FAKE_NAV_INFO: InfoDict = {
    "SSP_longitude": 140.65699999999998,
    "SSP_latitude": 0.0042985719753897015,
    "distance_earth_center_to_satellite": 42165.04,
    "nadir_longitude": 140.25253875463318,
    "nadir_latitude": 0.01674775121155575,
}
FAKE_CAL_INFO: InfoDict = {"blocklength": 0, "band_number": [4]}
FAKE_IRVISCAL_INFO: InfoDict = {}
FAKE_INTERCAL_INFO: InfoDict = {"blocklength": 0}
FAKE_SEGMENT_INFO: InfoDict = {"blocklength": 0}
FAKE_NAVCORR_INFO: InfoDict = {"blocklength": 0, "numof_correction_info_data": [1]}
FAKE_NAVCORR_SUBINFO: InfoDict = {}
FAKE_OBS_TIME_INFO: InfoDict = {"blocklength": 0, "number_of_observation_times": [1]}
FAKE_OBS_LINETIME_INFO: InfoDict = {}
FAKE_ERROR_INFO: InfoDict = {"blocklength": 0, "number_of_error_info_data": [1]}
FAKE_ERROR_LINE_INFO: InfoDict = {}
FAKE_SPARE_INFO: InfoDict = {"blocklength": 0}


def _new_unzip(fname, prefix=""):
    """Fake unzipping."""
    if fname[-3:] == "bz2":
        return prefix + fname[:-4]
    return fname


class TestAHIHSDNavigation(unittest.TestCase):
    """Test the AHI HSD reader navigation."""

    @mock.patch("satpy.readers.ahi_hsd.np2str")
    @mock.patch("satpy.readers.ahi_hsd.np.fromfile")
    def test_region(self, fromfile, np2str):
        """Test region navigation."""
        from pyproj import CRS

        np2str.side_effect = lambda x: x
        m = mock.mock_open()
        with mock.patch("satpy.readers.ahi_hsd.open", m, create=True):
            fh = AHIHSDFileHandler("somefile",
                                   {"segment": 1, "total_segments": 1},
                                   filetype_info={"file_type": "hsd_b01"},
                                   user_calibration=None)
            fh.proj_info = {"CFAC": 40932549,
                            "COFF": -591.5,
                            "LFAC": 40932549,
                            "LOFF": 5132.5,
                            "blocklength": 127,
                            "coeff_for_sd": 1737122264.0,
                            "distance_from_earth_center": 42164.0,
                            "earth_equatorial_radius": 6378.137,
                            "earth_polar_radius": 6356.7523,
                            "hblock_number": 3,
                            "req2_rpol2": 1.006739501,
                            "req2_rpol2_req2": 0.0066943844,
                            "resampling_size": 4,
                            "resampling_types": 0,
                            "rpol2_req2": 0.993305616,
                            "spare": "",
                            "sub_lon": 140.7}

            fh.data_info = {"blocklength": 50,
                            "compression_flag_for_data": 0,
                            "hblock_number": 2,
                            "number_of_bits_per_pixel": 16,
                            "number_of_columns": np.array([1000]),
                            "number_of_lines": np.array([1000]),
                            "spare": ""}

            area_def = fh.get_area_def(None)
            expected_crs = CRS.from_dict(dict(a=6378137.0, b=6356752.3, h= 35785863.0,
                                              lon_0=140.7, proj="geos", units="m"))
            assert area_def.crs == expected_crs
            np.testing.assert_allclose(area_def.area_extent, (592000.0038256242, 4132000.0267018233,
                                                              1592000.0102878273, 5132000.033164027))

    @mock.patch("satpy.readers.ahi_hsd.np2str")
    @mock.patch("satpy.readers.ahi_hsd.np.fromfile")
    def test_segment(self, fromfile, np2str):
        """Test segment navigation."""
        from pyproj import CRS

        np2str.side_effect = lambda x: x
        m = mock.mock_open()
        with mock.patch("satpy.readers.ahi_hsd.open", m, create=True):
            fh = AHIHSDFileHandler("somefile", {"segment": 8, "total_segments": 10},
                                   filetype_info={"file_type": "hsd_b01"})
            fh.proj_info = {"CFAC": 40932549,
                            "COFF": 5500.5,
                            "LFAC": 40932549,
                            "LOFF": 5500.5,
                            "blocklength": 127,
                            "coeff_for_sd": 1737122264.0,
                            "distance_from_earth_center": 42164.0,
                            "earth_equatorial_radius": 6378.137,
                            "earth_polar_radius": 6356.7523,
                            "hblock_number": 3,
                            "req2_rpol2": 1.006739501,
                            "req2_rpol2_req2": 0.0066943844,
                            "resampling_size": 4,
                            "resampling_types": 0,
                            "rpol2_req2": 0.993305616,
                            "spare": "",
                            "sub_lon": 140.7}

            fh.data_info = {"blocklength": 50,
                            "compression_flag_for_data": 0,
                            "hblock_number": 2,
                            "number_of_bits_per_pixel": 16,
                            "number_of_columns": np.array([11000]),
                            "number_of_lines": np.array([1100]),
                            "spare": ""}

            area_def = fh.get_area_def(None)
            expected_crs = CRS.from_dict(dict(a=6378137.0, b=6356752.3, h= 35785863.0,
                                              lon_0=140.7, proj="geos", units="m"))
            assert area_def.crs == expected_crs
            np.testing.assert_allclose(area_def.area_extent, (-5500000.035542117, -3300000.021325271,
                                                              5500000.035542117, -2200000.0142168473))


@pytest.fixture
def hsd_file_jp01(tmp_path):
    """Create a jp01 hsd file."""
    from satpy.readers.ahi_hsd import (  # _IRCAL_INFO_TYPE,
        _BASIC_INFO_TYPE,
        _CAL_INFO_TYPE,
        _DATA_INFO_TYPE,
        _ERROR_INFO_TYPE,
        _ERROR_LINE_INFO_TYPE,
        _INTER_CALIBRATION_INFO_TYPE,
        _NAV_INFO_TYPE,
        _NAVIGATION_CORRECTION_INFO_TYPE,
        _NAVIGATION_CORRECTION_SUBINFO_TYPE,
        _OBSERVATION_LINE_TIME_INFO_TYPE,
        _OBSERVATION_TIME_INFO_TYPE,
        _PROJ_INFO_TYPE,
        _SEGMENT_INFO_TYPE,
        _SPARE_TYPE,
        _VISCAL_INFO_TYPE,
    )
    nrows = 11000
    ncols = 11000
    filename = tmp_path / "somedata.DAT"
    error_lines = 0
    number_nav_corrections = 0
    number_observation_times = 6
    dat_type = np.dtype([("block1", _BASIC_INFO_TYPE),
                         ("block2", _DATA_INFO_TYPE),
                         ("block3", _PROJ_INFO_TYPE),
                         ("block4", _NAV_INFO_TYPE),
                         ("block5", _CAL_INFO_TYPE),
                         ("calibration", _VISCAL_INFO_TYPE),
                         ("block6", _INTER_CALIBRATION_INFO_TYPE),
                         ("block7", _SEGMENT_INFO_TYPE),
                         ("block8", _NAVIGATION_CORRECTION_INFO_TYPE),
                         ("navigation_corrections", _NAVIGATION_CORRECTION_SUBINFO_TYPE,
                          (number_nav_corrections,)),
                         ("block9", _OBSERVATION_TIME_INFO_TYPE),
                         ("observation_time_information", _OBSERVATION_LINE_TIME_INFO_TYPE,
                          (number_observation_times,)),
                         ("block10", _ERROR_INFO_TYPE),
                         ("error_info", _ERROR_LINE_INFO_TYPE, (error_lines,)),
                         ("block11", _SPARE_TYPE),
                         ("image", "<u2", (nrows, ncols))])
    dat = np.zeros(1, dat_type)
    dat["block1"]["blocklength"] = _BASIC_INFO_TYPE.itemsize
    dat["block1"]["observation_area"] = "JP01"
    dat["block1"]["satellite"] = b"Himawari-8"
    dat["block2"]["blocklength"] = _DATA_INFO_TYPE.itemsize
    dat["block2"]["number_of_lines"] = nrows
    dat["block2"]["number_of_columns"] = ncols
    dat["block3"]["blocklength"] = _PROJ_INFO_TYPE.itemsize
    dat["block3"]["sub_lon"] = 140.7
    dat["block3"]["CFAC"] = 81865099
    dat["block3"]["LFAC"] = 81865099
    dat["block3"]["COFF"] = 5500.5
    dat["block3"]["LOFF"] = 5500.5
    dat["block3"]["distance_from_earth_center"] = 42164.
    dat["block3"]["earth_equatorial_radius"] = 6378.137
    dat["block3"]["earth_polar_radius"] = 6356.7523
    dat["block4"]["blocklength"] = _NAV_INFO_TYPE.itemsize
    dat["block5"]["blocklength"] = _CAL_INFO_TYPE.itemsize + _VISCAL_INFO_TYPE.itemsize
    dat["block6"]["blocklength"] = _INTER_CALIBRATION_INFO_TYPE.itemsize
    dat["block7"]["blocklength"] = _SEGMENT_INFO_TYPE.itemsize
    dat["block8"]["blocklength"] = (_NAVIGATION_CORRECTION_INFO_TYPE.itemsize +
                                    number_nav_corrections * _NAVIGATION_CORRECTION_SUBINFO_TYPE.itemsize)
    dat["block9"]["blocklength"] = (_OBSERVATION_TIME_INFO_TYPE.itemsize +
                                    number_observation_times * _OBSERVATION_LINE_TIME_INFO_TYPE.itemsize)
    dat["block10"]["blocklength"] = _ERROR_INFO_TYPE.itemsize + error_lines * _ERROR_LINE_INFO_TYPE.itemsize
    dat["block11"]["blocklength"] = _SPARE_TYPE.itemsize
    dat.tofile(filename)
    return filename


class TestAHIHSDFileHandler:
    """Tests for the AHI HSD file handler."""

    def test_bad_calibration(self):
        """Test that a bad calibration mode causes an exception."""
        with pytest.raises(ValueError, match="Invalid calibration mode: BAD_MODE. Choose one of (.*)"):
            with _fake_hsd_handler(fh_kwargs={"calib_mode": "BAD_MODE"}):
                pass

    @pytest.mark.parametrize(
        ("round_actual_position", "expected_result"),
        [
            (False, (140.65699999999998, 0.0042985719753897015, 35786903.00011936)),
            (True, (140.657, 0.0, 35786850.0))
        ]
    )
    def test_actual_satellite_position(self, round_actual_position, expected_result):
        """Test that rounding of the actual satellite position can be controlled."""
        with _fake_hsd_handler(fh_kwargs={"round_actual_position": round_actual_position}) as fh:
            ds_id = make_dataid(name="B01", resolution=1000)
            ds_info = {
                "units": "%",
                "standard_name": "some_name",
                "wavelength": (0.1, 0.2, 0.3),
                "resolution": 1000,
            }
            metadata = fh._get_metadata(ds_id, ds_info)
            orb_params = metadata["orbital_parameters"]
            assert orb_params["satellite_actual_longitude"] == expected_result[0]
            assert orb_params["satellite_actual_latitude"] == expected_result[1]
            assert orb_params["satellite_actual_altitude"] == expected_result[2]

    @mock.patch("satpy.readers.ahi_hsd.AHIHSDFileHandler._check_fpos")
    def test_read_header(self, *mocks):
        """Test header reading."""
        with _fake_hsd_handler() as fh:
            fh._read_header(mock.MagicMock())

    @mock.patch("satpy.readers.ahi_hsd.AHIHSDFileHandler._read_data")
    @mock.patch("satpy.readers.ahi_hsd.AHIHSDFileHandler._mask_invalid")
    @mock.patch("satpy.readers.ahi_hsd.AHIHSDFileHandler.calibrate")
    def test_read_band(self, calibrate, *mocks):
        """Test masking of space pixels."""
        nrows = 25
        ncols = 100
        calibrate.return_value = np.ones((nrows, ncols))
        with _fake_hsd_handler() as fh:
            fh.data_info["number_of_columns"] = ncols
            fh.data_info["number_of_lines"] = nrows
            with warnings.catch_warnings():
                # The header isn't valid
                warnings.filterwarnings("ignore", category=UserWarning, message=r"Actual .* header size")
                im = fh.read_band(mock.MagicMock(), mock.MagicMock())
            # Note: Within the earth's shape get_geostationary_mask() is True but the numpy.ma mask
            # is False
            mask = im.to_masked_array().mask
            ref_mask = np.logical_not(get_geostationary_mask(fh.area).compute())
            np.testing.assert_equal(mask, ref_mask)

            # Test attributes
            orb_params_exp = {"projection_longitude": 140.7,
                              "projection_latitude": 0.,
                              "projection_altitude": 35785863.0,
                              "satellite_actual_longitude": 140.657,
                              "satellite_actual_latitude": 0.0,
                              "satellite_actual_altitude": 35786850,
                              "nadir_longitude": 140.252539,
                              "nadir_latitude": 0.01674775}
            actual_obs_params = im.attrs["orbital_parameters"]
            for key, value in orb_params_exp.items():
                assert key in actual_obs_params
                np.testing.assert_allclose(value, actual_obs_params[key])

            time_params_exp = {
                "nominal_start_time": dt.datetime(2018, 10, 22, 3, 0, 0, 0),
                "nominal_end_time": dt.datetime(2018, 10, 22, 3, 10, 0, 0),
                "observation_start_time": dt.datetime(2018, 10, 22, 3, 0, 20, 596896),
                "observation_end_time": dt.datetime(2018, 10, 22, 3, 10, 20, 596896),
            }
            actual_time_params = im.attrs["time_parameters"]
            for key, value in time_params_exp.items():
                assert key in actual_time_params
                assert value == actual_time_params[key]

            # Test if masking space pixels disables with appropriate flag
            fh.mask_space = False
            with mock.patch("satpy.readers.ahi_hsd.AHIHSDFileHandler._mask_space") as mask_space:
                with warnings.catch_warnings():
                    # The header isn't valid
                    warnings.filterwarnings("ignore", category=UserWarning, message=r"Actual .* header size")
                    fh.read_band(mock.MagicMock(), mock.MagicMock())
                mask_space.assert_not_called()

    def test_read_band_from_actual_file(self, hsd_file_jp01):
        """Test read_bands on real data."""
        filename_info = {"segment": 1, "total_segments": 1}
        filetype_info = {"file_type": "blahB01"}
        fh = AHIHSDFileHandler(hsd_file_jp01, filename_info, filetype_info)
        key = {"name": "B01", "calibration": "counts", "resolution": 1000}
        import dask
        with dask.config.set({"array.chunk-size": "32MiB"}):
            with warnings.catch_warnings():
                # The header isn't valid
                warnings.filterwarnings("ignore", category=UserWarning, message=r"Actual .* header size")
                data = fh.read_band(
                    key,
                    {
                        "units": "%",
                        "standard_name": "toa_bidirectional_reflectance",
                        "wavelength": 2,
                        "resolution": 1000,
                    })
            assert data.chunks == ((1100,) * 10, (1100,) * 10)
            assert data.dtype == data.compute().dtype
            assert data.dtype == np.float32

    @mock.patch("satpy.readers.ahi_hsd.AHIHSDFileHandler._read_data")
    @mock.patch("satpy.readers.ahi_hsd.AHIHSDFileHandler._mask_invalid")
    @mock.patch("satpy.readers.ahi_hsd.AHIHSDFileHandler.calibrate")
    def test_scene_loading(self, calibrate, *mocks):
        """Test masking of space pixels."""
        from satpy import Scene
        nrows = 25
        ncols = 100
        calibrate.return_value = np.ones((nrows, ncols))
        with _fake_hsd_handler() as fh:
            with mock.patch("satpy.readers.ahi_hsd.AHIHSDFileHandler") as fh_cls:
                fh_cls.return_value = fh
                fh.filename_info["total_segments"] = 1
                fh.filename_info["segment"] = 1
                fh.data_info["number_of_columns"] = ncols
                fh.data_info["number_of_lines"] = nrows
                scn = Scene(reader="ahi_hsd", filenames=["HS_H08_20210225_0700_B07_FLDK_R20_S0110.DAT"])
                with warnings.catch_warnings():
                    # The header isn't valid
                    warnings.filterwarnings("ignore", category=UserWarning, message=r"Actual .* header size")
                    scn.load(["B07"])
                im = scn["B07"]

                # Make sure space masking worked
                mask = im.to_masked_array().mask
                ref_mask = np.logical_not(get_geostationary_mask(fh.area).compute())
                np.testing.assert_equal(mask, ref_mask)

                # Make sure proj_id is correct
                assert fh.area.proj_id == f'geosh{FAKE_BASIC_INFO["satellite"][-1]}'

    def test_time_properties(self):
        """Test start/end/scheduled time properties."""
        with _fake_hsd_handler() as fh:
            assert fh.start_time == dt.datetime(2018, 10, 22, 3, 0)
            assert fh.end_time == dt.datetime(2018, 10, 22, 3, 10)
            assert fh.observation_start_time == dt.datetime(2018, 10, 22, 3, 0, 20, 596896)
            assert fh.observation_end_time == dt.datetime(2018, 10, 22, 3, 10, 20, 596896)
            assert fh.nominal_start_time == dt.datetime(2018, 10, 22, 3, 0, 0, 0)
            assert fh.nominal_end_time == dt.datetime(2018, 10, 22, 3, 10, 0, 0)

    def test_blocklen_error(self, *mocks):
        """Test erraneous blocklength."""
        open_name = "%s.open" % __name__
        fpos = 50
        with _fake_hsd_handler() as fh, \
                mock.patch(open_name, create=True) as mock_open, \
                mock_open(mock.MagicMock(), "r") as fp_:
            # Expected and actual blocklength match
            fp_.tell.return_value = 50
            with warnings.catch_warnings(record=True) as w:
                fh._check_fpos(fp_, fpos, 0, "header 1")
                assert len(w) == 0

            # Expected and actual blocklength do not match
            fp_.tell.return_value = 100
            with pytest.warns(UserWarning, match=r"Actual .* header size does not match expected"):
                fh._check_fpos(fp_, fpos, 0, "header 1")


class TestAHICalibration(unittest.TestCase):
    """Test case for various AHI calibration types."""

    @mock.patch("satpy.readers.ahi_hsd.AHIHSDFileHandler.__init__",
                return_value=None)
    def setUp(self, *mocks):
        """Create fake data for testing."""
        self.def_cali = [-0.0037, 15.20]
        self.upd_cali = [-0.0074, 30.40]
        self.bad_cali = [0.0, 0.0]
        fh = AHIHSDFileHandler(filetype_info={"file_type": "hsd_b01"})
        fh.calib_mode = "NOMINAL"
        fh.user_calibration = None
        fh.is_zipped = False
        fh._header = {
            "block5": {"band_number": [5],
                       "gain_count2rad_conversion": [self.def_cali[0]],
                       "offset_count2rad_conversion": [self.def_cali[1]],
                       "central_wave_length": [10.4073], },
            "calibration": {"coeff_rad2albedo_conversion": [0.0019255],
                            "speed_of_light": [299792458.0],
                            "planck_constant": [6.62606957e-34],
                            "boltzmann_constant": [1.3806488e-23],
                            "c0_rad2tb_conversion": [-0.116127314574],
                            "c1_rad2tb_conversion": [1.00099153832],
                            "c2_rad2tb_conversion": [-1.76961091571e-06],
                            "cali_gain_count2rad_conversion": [self.upd_cali[0]],
                            "cali_offset_count2rad_conversion": [self.upd_cali[1]]},
        }

        self.counts = da.array(np.array([[0, 1000],
                                         [2000, 5000]], dtype=np.uint16))
        self.fh = fh

    def test_default_calibrate(self, *mocks):
        """Test default in-file calibration modes."""
        self.setUp()
        # Counts
        assert self.fh.calibrate(data=123, calibration="counts") == 123

        # Radiance
        rad_exp = np.array([[15.2, 11.5],
                            [7.8, -3.3]])
        rad = self.fh.calibrate(data=self.counts,
                                calibration="radiance")
        assert np.allclose(rad, rad_exp)

        # Brightness Temperature
        bt_exp = np.array([[330.978979, 310.524688],
                           [285.845017, np.nan]])
        bt = self.fh.calibrate(data=self.counts,
                               calibration="brightness_temperature")
        np.testing.assert_allclose(bt, bt_exp)

        # Reflectance
        refl_exp = np.array([[2.92676, 2.214325],
                             [1.50189, 0.]])
        refl = self.fh.calibrate(data=self.counts,
                                 calibration="reflectance")
        assert np.allclose(refl, refl_exp)

    def test_updated_calibrate(self):
        """Test updated in-file calibration modes."""
        # Standard operation
        self.fh.calib_mode = "UPDATE"
        rad_exp = np.array([[30.4, 23.0],
                            [15.6, -6.6]])
        rad = self.fh.calibrate(data=self.counts, calibration="radiance")
        assert np.allclose(rad, rad_exp)

        # Case for no updated calibration available (older data)
        self.fh._header = {
            "block5": {"band_number": [5],
                       "gain_count2rad_conversion": [self.def_cali[0]],
                       "offset_count2rad_conversion": [self.def_cali[1]],
                       "central_wave_length": [10.4073], },
            "calibration": {"coeff_rad2albedo_conversion": [0.0019255],
                            "speed_of_light": [299792458.0],
                            "planck_constant": [6.62606957e-34],
                            "boltzmann_constant": [1.3806488e-23],
                            "c0_rad2tb_conversion": [-0.116127314574],
                            "c1_rad2tb_conversion": [1.00099153832],
                            "c2_rad2tb_conversion": [-1.76961091571e-06],
                            "cali_gain_count2rad_conversion": [self.bad_cali[0]],
                            "cali_offset_count2rad_conversion": [self.bad_cali[1]]},
        }
        rad = self.fh.calibrate(data=self.counts, calibration="radiance")
        rad_exp = np.array([[15.2, 11.5],
                            [7.8, -3.3]])
        assert np.allclose(rad, rad_exp)

    def test_user_calibration(self):
        """Test user-defined calibration modes."""
        # This is for radiance correction
        self.fh.user_calibration = {"B13": {"slope": 0.95,
                                            "offset": -0.1}}
        self.fh.band_name = "B13"
        rad = self.fh.calibrate(data=self.counts, calibration="radiance")
        rad_np = rad.compute()
        assert rad.dtype == rad_np.dtype
        assert rad.dtype == np.float32
        rad_exp = np.array([[16.10526316, 12.21052632],
                            [8.31578947, -3.36842105]])
        assert np.allclose(rad, rad_exp)

        # This is for DN calibration
        self.fh.user_calibration = {"B13": {"slope": -0.0032,
                                            "offset": 15.20},
                                    "type": "DN"}
        self.fh.band_name = "B13"
        rad = self.fh.calibrate(data=self.counts, calibration="radiance")
        rad_np = rad.compute()
        assert rad.dtype == rad_np.dtype
        assert rad.dtype == np.float32
        rad_exp = np.array([[15.2, 12.],
                            [8.8, -0.8]])
        assert np.allclose(rad, rad_exp)


@contextlib.contextmanager
def _fake_hsd_handler(fh_kwargs=None):
    """Create a test file handler."""
    m = mock.mock_open()
    with mock.patch("satpy.readers.ahi_hsd.np.fromfile", _custom_fromfile), \
            mock.patch("satpy.readers.ahi_hsd.unzip_file", mock.MagicMock(side_effect=_new_unzip)), \
            mock.patch("satpy.readers.ahi_hsd.open", m, create=True):
        in_fname = "test_file.bz2"
        fh = _create_fake_file_handler(in_fname, fh_kwargs=fh_kwargs)
        yield fh


def _custom_fromfile(*args, **kwargs):
    from satpy.readers.ahi_hsd import (
        _BASIC_INFO_TYPE,
        _CAL_INFO_TYPE,
        _DATA_INFO_TYPE,
        _ERROR_INFO_TYPE,
        _ERROR_LINE_INFO_TYPE,
        _INTER_CALIBRATION_INFO_TYPE,
        _IRCAL_INFO_TYPE,
        _NAV_INFO_TYPE,
        _NAVIGATION_CORRECTION_INFO_TYPE,
        _NAVIGATION_CORRECTION_SUBINFO_TYPE,
        _OBSERVATION_LINE_TIME_INFO_TYPE,
        _OBSERVATION_TIME_INFO_TYPE,
        _PROJ_INFO_TYPE,
        _SEGMENT_INFO_TYPE,
        _SPARE_TYPE,
        _VISCAL_INFO_TYPE,
    )
    dtype = kwargs.get("dtype")
    fake_info_map = {
        _BASIC_INFO_TYPE: FAKE_BASIC_INFO,
        _DATA_INFO_TYPE: FAKE_DATA_INFO,
        _NAV_INFO_TYPE: FAKE_NAV_INFO,
        _PROJ_INFO_TYPE: FAKE_PROJ_INFO,
        _CAL_INFO_TYPE: FAKE_CAL_INFO,
        _VISCAL_INFO_TYPE: FAKE_IRVISCAL_INFO,
        _IRCAL_INFO_TYPE: FAKE_IRVISCAL_INFO,
        _INTER_CALIBRATION_INFO_TYPE: FAKE_INTERCAL_INFO,
        _SEGMENT_INFO_TYPE: FAKE_SEGMENT_INFO,
        _NAVIGATION_CORRECTION_INFO_TYPE: FAKE_NAVCORR_INFO,
        _NAVIGATION_CORRECTION_SUBINFO_TYPE: FAKE_NAVCORR_SUBINFO,
        _OBSERVATION_TIME_INFO_TYPE: FAKE_OBS_TIME_INFO,
        _OBSERVATION_LINE_TIME_INFO_TYPE: FAKE_OBS_LINETIME_INFO,
        _ERROR_INFO_TYPE: FAKE_ERROR_INFO,
        _ERROR_LINE_INFO_TYPE: FAKE_ERROR_LINE_INFO,
        _SPARE_TYPE: FAKE_SPARE_INFO,
    }
    info_dict = fake_info_map[dtype]
    fake_arr = np.zeros((1,), dtype=dtype)
    for key, value in info_dict.items():
        fake_arr[key] = value
    return fake_arr


def _create_fake_file_handler(in_fname, filename_info=None, filetype_info=None, fh_kwargs=None):
    if filename_info is None:
        filename_info = {"segment": 8, "total_segments": 10}
    if filetype_info is None:
        filetype_info = {"file_type": "hsd_b01"}
    if fh_kwargs is None:
        fh_kwargs = {}
    fh = AHIHSDFileHandler(in_fname, filename_info, filetype_info, **fh_kwargs)

    # Check that the filename is altered and 2 digit segment prefix added for bz2 format files
    assert in_fname != fh.filename
    assert str(filename_info["segment"]).zfill(2) == fh.filename[0:2]
    return fh


class TestNominalTimeCalculator:
    """Test case for nominal timestamp computation."""

    @pytest.mark.parametrize(
        ("timeline", "expected"),
        [
            ("0300", dt.datetime(2020, 1, 1, 3, 0, 0)),
            ("65526", dt.datetime(2020, 1, 1, 12, 0, 0))
        ]
    )
    def test_invalid_timeline(self, timeline, expected):
        """Test handling of invalid timeline."""
        calc = _NominalTimeCalculator(timeline, "FLDK")
        res = calc.get_nominal_start_time(dt.datetime(2020, 1, 1, 12, 0, 0))
        assert res == expected

    @pytest.mark.parametrize(
        ("area", "expected"),
        [
            (
                "JP01",
                {"tstart": dt.datetime(2018, 10, 22, 3, 0, 0),
                 "tend": dt.datetime(2018, 10, 22, 3, 2, 30)}
            ),
            (
                "JP04",
                {"tstart": dt.datetime(2018, 10, 22, 3, 7, 30, 0),
                 "tend": dt.datetime(2018, 10, 22, 3, 10, 0, 0)}
            ),
            (
                "R301",
                {"tstart": dt.datetime(2018, 10, 22, 3, 0, 0),
                 "tend": dt.datetime(2018, 10, 22, 3, 2, 30)}
            ),
            (
                "R304",
                {"tstart": dt.datetime(2018, 10, 22, 3, 7, 30, 0),
                 "tend": dt.datetime(2018, 10, 22, 3, 10, 0, 0)}
            ),
            (
                "R401",
                {"tstart": dt.datetime(2018, 10, 22, 3, 0, 0),
                 "tend": dt.datetime(2018, 10, 22, 3, 0, 30)}
            ),
            (
                "R420",
                {"tstart": dt.datetime(2018, 10, 22, 3, 9, 30, 0),
                 "tend": dt.datetime(2018, 10, 22, 3, 10, 0, 0)}
            ),
            (
                "R501",
                {"tstart": dt.datetime(2018, 10, 22, 3, 0, 0),
                 "tend": dt.datetime(2018, 10, 22, 3, 0, 30)}
            ),
            (
                "R520",
                {"tstart": dt.datetime(2018, 10, 22, 3, 9, 30, 0),
                 "tend": dt.datetime(2018, 10, 22, 3, 10, 0, 0)}
            ),
        ]
    )
    def test_areas(self, area, expected):
        """Test nominal timestamps for multiple areas."""
        obs_start_time = dt.datetime(2018, 10, 22, 3, 0, 20, 596896)
        calc = _NominalTimeCalculator("0300", area)
        nom_start_time = calc.get_nominal_start_time(obs_start_time)
        nom_end_time = calc.get_nominal_end_time(nom_start_time)
        assert nom_start_time == expected["tstart"]
        assert nom_end_time == expected["tend"]

    @pytest.mark.parametrize(
        ("timeline", "obs_start_time", "expected"),
        [
            (
                "2350",
                dt.datetime(2022, 12, 31, 23, 50, 1),
                {"tstart": dt.datetime(2022, 12, 31, 23, 50, 0),
                 "tend": dt.datetime(2023, 1, 1, 0, 0, 0)}
            ),
            (
                "2350",
                dt.datetime(2022, 12, 31, 23, 49, 59),
                {"tstart": dt.datetime(2022, 12, 31, 23, 50, 0),
                 "tend": dt.datetime(2023, 1, 1, 0, 0, 0)}
            ),
            (
                "0000",
                dt.datetime(2023, 1, 1, 0, 0, 1),
                {"tstart": dt.datetime(2023, 1, 1, 0, 0, 0),
                 "tend": dt.datetime(2023, 1, 1, 0, 10, 0)}
            ),
            (
                "0000",
                dt.datetime(2022, 12, 31, 23, 59, 59),
                {"tstart": dt.datetime(2023, 1, 1, 0, 0, 0),
                 "tend": dt.datetime(2023, 1, 1, 0, 10, 0)}
            ),
        ]
    )
    def test_timelines(self, timeline, obs_start_time, expected):
        """Test nominal timestamps for multiple timelines."""
        calc = _NominalTimeCalculator(timeline, "FLDK")
        nom_start_time = calc.get_nominal_start_time(obs_start_time)
        nom_end_time = calc.get_nominal_end_time(nom_start_time)
        assert nom_start_time == expected["tstart"]
        assert nom_end_time == expected["tend"]
