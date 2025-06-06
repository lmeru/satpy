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
"""Module for testing the satpy.readers.amsr2_l1b module."""

import os
import unittest
from unittest import mock

import numpy as np

from satpy.tests.reader_tests.test_hdf5_utils import FakeHDF5FileHandler
from satpy.tests.utils import convert_file_content_to_data_array

DEFAULT_FILE_DTYPE = np.uint16
DEFAULT_FILE_SHAPE = (10, 300)
DEFAULT_FILE_DATA = np.arange(DEFAULT_FILE_SHAPE[0] * DEFAULT_FILE_SHAPE[1],
                              dtype=DEFAULT_FILE_DTYPE).reshape(DEFAULT_FILE_SHAPE)
DEFAULT_FILE_FACTORS = np.array([2.0, 1.0], dtype=np.float32)
DEFAULT_LAT_DATA = np.linspace(45, 65, DEFAULT_FILE_SHAPE[1]).astype(DEFAULT_FILE_DTYPE)
DEFAULT_LAT_DATA = np.repeat([DEFAULT_LAT_DATA], DEFAULT_FILE_SHAPE[0], axis=0)
DEFAULT_LON_DATA = np.linspace(5, 45, DEFAULT_FILE_SHAPE[1]).astype(DEFAULT_FILE_DTYPE)
DEFAULT_LON_DATA = np.repeat([DEFAULT_LON_DATA], DEFAULT_FILE_SHAPE[0], axis=0)


class FakeHDF5FileHandler2(FakeHDF5FileHandler):
    """Swap-in HDF5 File Handler."""

    def get_test_content(self, filename, filename_info, filetype_info):
        """Mimic reader input file content."""
        file_content = {
            "/attr/PlatformShortName": "GCOM-W1",
            "/attr/SensorShortName": "AMSR2",
            "/attr/StartOrbitNumber": "22210",
            "/attr/StopOrbitNumber": "22210",
        }
        for bt_chan in [
            "(10.7GHz,H)",
            "(10.7GHz,V)",
            "(18.7GHz,H)",
            "(18.7GHz,V)",
            "(23.8GHz,H)",
            "(23.8GHz,V)",
            "(36.5GHz,H)",
            "(36.5GHz,V)",
            "(6.9GHz,H)",
            "(6.9GHz,V)",
            "(7.3GHz,H)",
            "(7.3GHz,V)",
            "(89.0GHz-A,H)",
            "(89.0GHz-A,V)",
            "(89.0GHz-B,H)",
            "(89.0GHz-B,V)",
        ]:
            k = "Brightness Temperature {}".format(bt_chan)
            file_content[k] = DEFAULT_FILE_DATA[:, ::2]
            file_content[k + "/shape"] = (DEFAULT_FILE_SHAPE[0], DEFAULT_FILE_SHAPE[1] // 2)
            file_content[k + "/attr/UNIT"] = "K"
            file_content[k + "/attr/SCALE FACTOR"] = 0.01
        for bt_chan in [
            "(89.0GHz-A,H)",
            "(89.0GHz-A,V)",
            "(89.0GHz-B,H)",
            "(89.0GHz-B,V)",
        ]:
            k = "Brightness Temperature {}".format(bt_chan)
            file_content[k] = DEFAULT_FILE_DATA
            file_content[k + "/shape"] = DEFAULT_FILE_SHAPE
            file_content[k + "/attr/UNIT"] = "K"
            file_content[k + "/attr/SCALE FACTOR"] = 0.01
        for nav_chan in ["89A", "89B"]:
            lon_k = "Longitude of Observation Point for " + nav_chan
            lat_k = "Latitude of Observation Point for " + nav_chan
            file_content[lon_k] = DEFAULT_LON_DATA
            file_content[lon_k + "/shape"] = DEFAULT_FILE_SHAPE
            file_content[lon_k + "/attr/SCALE FACTOR"] = 1
            file_content[lon_k + "/attr/UNIT"] = "deg"
            file_content[lat_k] = DEFAULT_LAT_DATA
            file_content[lat_k + "/shape"] = DEFAULT_FILE_SHAPE
            file_content[lat_k + "/attr/SCALE FACTOR"] = 1
            file_content[lat_k + "/attr/UNIT"] = "deg"

        convert_file_content_to_data_array(file_content)
        return file_content


class TestAMSR2L1BReader(unittest.TestCase):
    """Test AMSR2 L1B Reader."""

    yaml_file = "amsr2_l1b.yaml"

    def setUp(self):
        """Wrap HDF5 file handler with our own fake handler."""
        from satpy._config import config_search_paths
        from satpy.readers.amsr2_l1b import AMSR2L1BFileHandler
        self.reader_configs = config_search_paths(os.path.join("readers", self.yaml_file))
        # http://stackoverflow.com/questions/12219967/how-to-mock-a-base-class-with-python-mock-library
        self.p = mock.patch.object(AMSR2L1BFileHandler, "__bases__", (FakeHDF5FileHandler2,))
        self.fake_handler = self.p.start()
        self.p.is_local = True

    def tearDown(self):
        """Stop wrapping the HDF5 file handler."""
        self.p.stop()

    def test_init(self):
        """Test basic init with no extra parameters."""
        from satpy.readers.core.loading import load_reader
        r = load_reader(self.reader_configs)
        loadables = r.select_files_from_pathnames([
            "GW1AM2_201607201808_128A_L1DLBTBR_1110110.h5",
        ])
        assert len(loadables) == 1
        r.create_filehandlers(loadables)
        # make sure we have some files
        assert r.file_handlers

    def test_load_basic(self):
        """Test loading of basic channels."""
        from satpy.readers.core.loading import load_reader
        r = load_reader(self.reader_configs)
        loadables = r.select_files_from_pathnames([
            "GW1AM2_201607201808_128A_L1DLBTBR_1110110.h5",
        ])
        assert len(loadables) == 1
        r.create_filehandlers(loadables)
        ds = r.load([
            "btemp_10.7v",
            "btemp_10.7h",
            "btemp_6.9v",
            "btemp_6.9h",
            "btemp_7.3v",
            "btemp_7.3h",
            "btemp_18.7v",
            "btemp_18.7h",
            "btemp_23.8v",
            "btemp_23.8h",
            "btemp_36.5v",
            "btemp_36.5h",
        ])
        assert len(ds) == 12
        for d in ds.values():
            assert d.attrs["calibration"] == "brightness_temperature"
            assert d.shape == (DEFAULT_FILE_SHAPE[0], int(DEFAULT_FILE_SHAPE[1] // 2))
            assert "area" in d.attrs
            assert d.attrs["area"] is not None
            assert d.attrs["area"].lons.shape == (DEFAULT_FILE_SHAPE[0], DEFAULT_FILE_SHAPE[1] // 2)
            assert d.attrs["area"].lats.shape == (DEFAULT_FILE_SHAPE[0], DEFAULT_FILE_SHAPE[1] // 2)
            assert d.attrs["sensor"] == "amsr2"
            assert d.attrs["platform_name"] == "GCOM-W1"

    def test_load_89ghz(self):
        """Test loading of 89GHz channels."""
        from satpy.readers.core.loading import load_reader
        r = load_reader(self.reader_configs)
        loadables = r.select_files_from_pathnames([
            "GW1AM2_201607201808_128A_L1DLBTBR_1110110.h5",
        ])
        assert len(loadables) == 1
        r.create_filehandlers(loadables)
        ds = r.load([
            "btemp_89.0av",
            "btemp_89.0ah",
            "btemp_89.0bv",
            "btemp_89.0bh",
        ])
        assert len(ds) == 4
        for d in ds.values():
            assert d.attrs["calibration"] == "brightness_temperature"
            assert d.shape == DEFAULT_FILE_SHAPE
            assert "area" in d.attrs
            assert d.attrs["area"] is not None
            assert d.attrs["area"].lons.shape == DEFAULT_FILE_SHAPE
            assert d.attrs["area"].lats.shape == DEFAULT_FILE_SHAPE
