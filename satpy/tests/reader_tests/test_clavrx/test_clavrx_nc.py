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
"""Module for testing the satpy.readers.clavrx module."""
import os
from unittest import mock

import numpy as np
import pytest
import xarray as xr
from pyresample.geometry import AreaDefinition

from satpy.readers.core.loading import load_reader

ABI_FILE = "clavrx_OR_ABI-L1b-RadC-M6C01_G16_s20231021601173.level2.nc"
DEFAULT_FILE_DTYPE = np.uint16
DEFAULT_FILE_SHAPE = (5, 5)
DEFAULT_FILE_DATA = np.arange(DEFAULT_FILE_SHAPE[0] * DEFAULT_FILE_SHAPE[1],
                              dtype=DEFAULT_FILE_DTYPE).reshape(DEFAULT_FILE_SHAPE)
DEFAULT_FILE_FLAGS = np.arange(DEFAULT_FILE_SHAPE[0] * DEFAULT_FILE_SHAPE[1],
                               dtype=np.byte).reshape(DEFAULT_FILE_SHAPE)
DEFAULT_FILE_FLAGS_BEYOND_FILL = DEFAULT_FILE_FLAGS
DEFAULT_FILE_FLAGS_BEYOND_FILL[-1][:-2] = [-127, -127, -128]
DEFAULT_FILE_FACTORS = np.array([2.0, 1.0], dtype=np.float32)
DEFAULT_LAT_DATA = np.linspace(45, 65, DEFAULT_FILE_SHAPE[1]).astype(DEFAULT_FILE_DTYPE)
DEFAULT_LAT_DATA = np.repeat([DEFAULT_LAT_DATA], DEFAULT_FILE_SHAPE[0], axis=0)
DEFAULT_LON_DATA = np.linspace(5, 45, DEFAULT_FILE_SHAPE[1]).astype(DEFAULT_FILE_DTYPE)
DEFAULT_LON_DATA = np.repeat([DEFAULT_LON_DATA], DEFAULT_FILE_SHAPE[0], axis=0)
L1B_FILE = "clavrx_OR_ABI-L1b-RadC-M6C01_G16_s20231021601173"
ABI_FILE = f"{L1B_FILE}.level2.nc"
FILL_VALUE = -32768


def fake_test_content(filename, **kwargs):
    """Mimic reader input file content."""
    attrs = {
        "platform": "G16",
        "sensor": "ABI",
        # this is a Level 2 file that came from a L1B file
        "L1B": L1B_FILE,
    }

    longitude = xr.DataArray(DEFAULT_LON_DATA,
                             dims=("scan_lines_along_track_direction",
                                   "pixel_elements_along_scan_direction"),
                             attrs={"_FillValue": -999.,
                                    "SCALED": 0,
                                    "scale_factor": 1.,
                                    "add_offset": 0.,
                                    "standard_name": "longitude",
                                    "units": "degrees_east"
                                    })

    latitude = xr.DataArray(DEFAULT_LAT_DATA,
                            dims=("scan_lines_along_track_direction",
                                  "pixel_elements_along_scan_direction"),
                            attrs={"_FillValue": -999.,
                                   "SCALED": 0,
                                   "scale_factor": 1.,
                                   "add_offset": 0.,
                                   "standard_name": "latitude",
                                   "units": "degrees_south"
                                   })

    variable1 = xr.DataArray(DEFAULT_FILE_DATA.astype(np.int8),
                             dims=("scan_lines_along_track_direction",
                                   "pixel_elements_along_scan_direction"),
                             attrs={"_FillValue": -127,
                                    "SCALED": 0,
                                    "units": "1",
                                    })

    # data with fill values and a file_type alias
    variable2 = xr.DataArray(DEFAULT_FILE_DATA.astype(np.int16),
                             dims=("scan_lines_along_track_direction",
                                   "pixel_elements_along_scan_direction"),
                             attrs={"_FillValue": FILL_VALUE,
                                    "SCALED": 1,
                                    "scale_factor": 0.001861629,
                                    "add_offset": 59.,
                                    "units": "%",
                                    "valid_range": [-32767, 32767],
        # this is a Level 2 file that came from a L1B file
        "L1B": "clavrx_H08_20210603_1500_B01_FLDK_R",
    }
    )
    variable2 = variable2.where(variable2 % 2 != 0, FILL_VALUE)

    # category
    var_flags = xr.DataArray(DEFAULT_FILE_FLAGS.astype(np.int8),
                             dims=("scan_lines_along_track_direction",
                                   "pixel_elements_along_scan_direction"),
                             attrs={"SCALED": 0,
                                    "_FillValue": -127,
                                    "units": "1",
                                    "flag_values": [0, 1, 2, 3]})

    out_of_range_flags = xr.DataArray(DEFAULT_FILE_FLAGS_BEYOND_FILL.astype(np.int8),
                                      dims=("scan_lines_along_track_direction",
                                            "pixel_elements_along_scan_direction"),
                                      attrs={"SCALED": 0,
                                             "_FillValue": -127,
                                             "units": "1",
                                             "flag_values": [0, 1, 2, 3]})

    ds_vars = {
        "longitude": longitude,
        "latitude": latitude,
        "variable1": variable1,
        "refl_0_65um_nom": variable2,
        "var_flags": var_flags,
        "out_of_range_flags": out_of_range_flags,
    }
    ds = xr.Dataset(ds_vars, attrs=attrs)
    ds = ds.assign_coords({"latitude": latitude, "longitude": longitude})

    return ds


class TestCLAVRXReaderGeo:
    """Test CLAVR-X Reader with Geo files."""

    yaml_file = "clavrx.yaml"

    def setup_method(self):
        """Read fake data."""
        from satpy._config import config_search_paths
        self.reader_configs = config_search_paths(os.path.join("readers", self.yaml_file))

    @pytest.mark.parametrize(
        ("filenames", "expected_loadables"),
        [([ABI_FILE], 1)]
    )
    def test_reader_creation(self, filenames, expected_loadables):
        """Test basic initialization."""
        with mock.patch("satpy.readers.clavrx.xr.open_dataset") as od:
            od.side_effect = fake_test_content
            r = load_reader(self.reader_configs)
            loadables = r.select_files_from_pathnames(filenames)
            assert len(loadables) == expected_loadables
            r.create_filehandlers(loadables)
            # make sure we have some files
            assert r.file_handlers

    @pytest.mark.parametrize(
        ("filenames", "expected_datasets"),
        [([ABI_FILE], ["variable1", "refl_0_65um_nom", "C02", "var_flags",
                       "out_of_range_flags", "longitude", "latitude"]), ]
    )
    def test_available_datasets(self, filenames, expected_datasets):
        """Test that variables are dynamically discovered."""
        from satpy.readers.core.loading import load_reader
        with mock.patch("satpy.readers.clavrx.xr.open_dataset") as od:
            od.side_effect = fake_test_content
            r = load_reader(self.reader_configs)
            loadables = r.select_files_from_pathnames(filenames)
            r.create_filehandlers(loadables)
            avails = list(r.available_dataset_names)
            for var_name in expected_datasets:
                assert var_name in avails

    @pytest.mark.parametrize(
        ("filenames", "loadable_ids"),
        [([ABI_FILE], ["variable1", "refl_0_65um_nom", "var_flags", "out_of_range_flags"]), ]
    )
    def test_load_all_new_donor(self, filenames, loadable_ids):
        """Test loading all test datasets with new donor."""
        with mock.patch("satpy.readers.clavrx.xr.open_dataset") as od:
            od.side_effect = fake_test_content
            r = load_reader(self.reader_configs)
            loadables = r.select_files_from_pathnames(filenames)
            r.create_filehandlers(loadables)

            with mock.patch("satpy.readers.clavrx.glob") as g, \
                    mock.patch("satpy.readers.clavrx.netCDF4.Dataset") as d:
                g.return_value = ["fake_donor.nc"]
                x = np.linspace(-0.1518, 0.1518, DEFAULT_FILE_SHAPE[1])
                y = np.linspace(0.1518, -0.1518, DEFAULT_FILE_SHAPE[0])
                proj = mock.Mock(
                    semi_major_axis=6378137,
                    semi_minor_axis=6356752.3142,
                    perspective_point_height=35791000,
                    longitude_of_projection_origin=140.7,
                    sweep_angle_axis="y",
                )
                d.return_value = fake_donor = mock.MagicMock(
                    variables={"goes_imager_projection": proj, "x": x, "y": y},
                )
                fake_donor.__getitem__.side_effect = lambda key: fake_donor.variables[key]

                datasets = r.load(loadable_ids + ["C02"])
                assert len(datasets) == len(loadable_ids)+1

                # should have file variable and one alias for reflectance
                assert "valid_range" not in datasets["variable1"].attrs
                assert "_FillValue" not in datasets["variable1"].attrs
                assert np.float32 == datasets["variable1"].dtype
                assert "valid_range" not in datasets["variable1"].attrs

                assert np.issubdtype(datasets["var_flags"].dtype, np.integer)
                assert datasets["var_flags"].attrs.get("flag_meanings") is not None
                assert "<flag_meanings_unknown>" == datasets["var_flags"].attrs.get("flag_meanings")
                assert np.issubdtype(datasets["out_of_range_flags"].dtype, np.integer)
                assert "valid_range" not in datasets["out_of_range_flags"].attrs

                assert isinstance(datasets["refl_0_65um_nom"].valid_range, list)
                assert np.float32 == datasets["refl_0_65um_nom"].dtype
                assert "_FillValue" not in datasets["refl_0_65um_nom"].attrs
                assert "valid_range" in datasets["refl_0_65um_nom"].attrs

                assert "refl_0_65um_nom" == datasets["C02"].file_key
                assert "_FillValue" not in datasets["C02"].attrs

                for v in datasets.values():
                    assert isinstance(v.area, AreaDefinition)
                    assert v.platform_name == "GOES-16"
                    assert v.sensor == "abi"

                    assert "calibration" not in v.attrs
                    assert "rows_per_scan" not in v.coords.get("longitude").attrs
                    assert "units" in v.attrs

    @pytest.mark.parametrize(
        ("filenames", "expected_loadables"),
        [([ABI_FILE], 1)]
    )
    def test_yaml_datasets(self, filenames, expected_loadables):
        """Test available_datasets with fake variables from YAML."""
        with mock.patch("satpy.readers.clavrx.xr.open_dataset") as od:
            od.side_effect = fake_test_content
            r = load_reader(self.reader_configs)
            loadables = r.select_files_from_pathnames(filenames)
            r.create_filehandlers(loadables)

            with mock.patch("satpy.readers.clavrx.glob") as g, \
                    mock.patch("satpy.readers.clavrx.netCDF4.Dataset") as d:
                g.return_value = ["fake_donor.nc"]
                x = np.linspace(-0.1518, 0.1518, 5)
                y = np.linspace(0.1518, -0.1518, 5)
                proj = mock.Mock(
                    semi_major_axis=6378137,
                    semi_minor_axis=6356752.3142,
                    perspective_point_height=35791000,
                    longitude_of_projection_origin=-137.2,
                    sweep_angle_axis="x",
                )
                d.return_value = fake_donor = mock.MagicMock(
                    variables={"goes_imager_projection": proj, "x": x, "y": y},
                )
                fake_donor.__getitem__.side_effect = lambda key: fake_donor.variables[key]
            # mimic the YAML file being configured for more datasets
            fake_dataset_info = [
                (None, {"name": "yaml1", "resolution": None, "file_type": ["clavrx_nc"]}),
                (True, {"name": "yaml2", "resolution": 0.5, "file_type": ["clavrx_nc"]}),
            ]
            new_ds_infos = list(r.file_handlers["clavrx_nc"][0].available_datasets(
                fake_dataset_info))
            assert len(new_ds_infos) == 10

            # we have this and can provide the resolution
            assert (new_ds_infos[0][0])
            assert new_ds_infos[0][1]["resolution"] == 2004  # hardcoded

            # we have this, but previous file handler said it knew about it
            # and it is producing the same resolution as what we have
            assert (new_ds_infos[1][0])
            assert new_ds_infos[1][1]["resolution"] == 0.5

            # we have this, but don"t want to change the resolution
            # because a previous handler said it has it
            assert (new_ds_infos[2][0])
            assert new_ds_infos[2][1]["resolution"] == 2004

    @pytest.mark.parametrize(
        ("filenames", "loadable_ids"),
        [([ABI_FILE], ["variable1", "refl_0_65um_nom", "var_flags", "out_of_range_flags"]), ]
    )
    def test_scale_data(self, filenames, loadable_ids):
        """Test that data is scaled when necessary and not scaled data are flags."""
        from satpy.readers.clavrx import _scale_data
        with mock.patch("satpy.readers.clavrx.xr.open_dataset") as od:
            od.side_effect = fake_test_content
            r = load_reader(self.reader_configs)
            loadables = r.select_files_from_pathnames(filenames)
            r.create_filehandlers(loadables)
        with mock.patch("satpy.readers.clavrx.glob") as g, \
                mock.patch("satpy.readers.clavrx.netCDF4.Dataset") as d:
            g.return_value = ["fake_donor.nc"]
            x = np.linspace(-0.1518, 0.1518, 5)
            y = np.linspace(0.1518, -0.1518, 5)
            proj = mock.Mock(
                semi_major_axis=6378137,
                semi_minor_axis=6356752.3142,
                perspective_point_height=35791000,
                longitude_of_projection_origin=-137.2,
                sweep_angle_axis="x",
            )
            d.return_value = fake_donor = mock.MagicMock(
                variables={"goes_imager_projection": proj, "x": x, "y": y},
            )
            fake_donor.__getitem__.side_effect = lambda key: fake_donor.variables[key]

            ds_scale = ["variable1", "refl_0_65um_nom"]
            ds_no_scale = ["var_flags", "out_of_range_flags"]

            with mock.patch("satpy.readers.clavrx._scale_data", wraps=_scale_data) as scale_data:
                r.load(ds_scale)
                scale_data.assert_called()

            with mock.patch("satpy.readers.clavrx._scale_data", wraps=_scale_data) as scale_data2:
                r.load(ds_no_scale)
                scale_data2.assert_not_called()
