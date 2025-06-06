#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2019 Satpy developers
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

"""Base HDF-EOS reader."""

from __future__ import annotations

import datetime as dt
import logging
import re
from ast import literal_eval
from contextlib import suppress
from functools import cache

import dask.array as da
import numpy as np
import xarray as xr
from pyhdf.error import HDF4Error
from pyhdf.SD import SD

from satpy import DataID
from satpy.readers.core.file_handlers import BaseFileHandler
from satpy.utils import normalize_low_res_chunks

logger = logging.getLogger(__name__)


def interpolate(clons, clats, csatz, src_resolution, dst_resolution):
    """Interpolate two parallel datasets jointly."""
    if csatz is None:
        return _interpolate_no_angles(clons, clats, src_resolution, dst_resolution)
    return _interpolate_with_angles(clons, clats, csatz, src_resolution, dst_resolution)


def _interpolate_with_angles(clons, clats, csatz, src_resolution, dst_resolution):
    from geotiepoints.modisinterpolator import modis_1km_to_250m, modis_1km_to_500m, modis_5km_to_1km

    # (src_res, dst_res, is satz not None) -> interp function
    interpolation_functions = {
        (5000, 1000): modis_5km_to_1km,
        (1000, 500): modis_1km_to_500m,
        (1000, 250): modis_1km_to_250m
    }
    return _find_and_run_interpolation(interpolation_functions, src_resolution, dst_resolution,
                                       (clons, clats, csatz))


def _interpolate_no_angles(clons, clats, src_resolution, dst_resolution):
    interpolation_functions = {}

    try:
        from geotiepoints.simple_modis_interpolator import modis_1km_to_250m as simple_1km_to_250m
        from geotiepoints.simple_modis_interpolator import modis_1km_to_500m as simple_1km_to_500m
    except ImportError:
        raise NotImplementedError(
            f"Interpolation from {src_resolution}m to {dst_resolution}m "
            "without satellite zenith angle information is not "
            "implemented. Try updating your version of "
            "python-geotiepoints.")
    else:
        interpolation_functions[(1000, 500)] = simple_1km_to_500m
        interpolation_functions[(1000, 250)] = simple_1km_to_250m

    return _find_and_run_interpolation(interpolation_functions, src_resolution, dst_resolution,
                                       (clons, clats))


def _find_and_run_interpolation(interpolation_functions, src_resolution, dst_resolution, args):
    try:
        interpolation_function = interpolation_functions[(src_resolution, dst_resolution)]
    except KeyError:
        error_message = "Interpolation from {}m to {}m not implemented".format(
            src_resolution, dst_resolution)
        raise NotImplementedError(error_message)

    logger.debug("Interpolating from {} to {}".format(src_resolution, dst_resolution))
    return interpolation_function(*args)

def _modis_date(date):
    """Transform a date and time string into a datetime object."""
    if len(date) == 19:
        return dt.datetime.strptime(date, "%Y-%m-%d %H:%M:%S")
    else:
        return dt.datetime.strptime(date, "%Y-%m-%d %H:%M:%S.%f")

class HDFEOSBaseFileReader(BaseFileHandler):
    """Base file handler for HDF EOS data for both L1b and L2 products."""

    def __init__(self, filename, filename_info, filetype_info, **kwargs):
        """Initialize the base reader."""
        BaseFileHandler.__init__(self, filename, filename_info, filetype_info)
        try:
            self.sd = SD(self.filename)
        except HDF4Error as err:
            error_message = "Could not load data from file {}: {}".format(self.filename, err)
            raise ValueError(error_message)

        self.metadata = self._load_all_metadata_attributes()

    def _load_all_metadata_attributes(self):
        metadata = {}
        attrs = self.sd.attributes()
        for md_key in ("CoreMetadata.0", "StructMetadata.0", "ArchiveMetadata.0"):
            try:
                str_val = attrs[md_key]
            except KeyError:
                continue
            else:
                metadata.update(self.read_mda(str_val))
        return metadata

    @classmethod
    def read_mda(cls, attribute):
        """Read the EOS metadata."""
        line_iterator = iter(attribute.split("\n"))
        return cls._read_mda(line_iterator)

    @classmethod
    def _read_mda(cls, lines, element=None):
        current_dict = {}

        for line in lines:
            if not line:
                continue
            if line == "END":
                return current_dict

            key, val = cls._split_line(line, lines)

            if key in ["GROUP", "OBJECT"]:
                current_dict[val] = cls._read_mda(lines, val)
            elif key in ["END_GROUP", "END_OBJECT"]:
                if val != element:
                    raise SyntaxError("Non-matching end-tag")
                return current_dict
            elif key in ["CLASS", "NUM_VAL"]:
                pass
            else:
                current_dict[key] = val
        logger.warning("Malformed EOS metadata, missing an END.")
        return current_dict

    @classmethod
    def _split_line(cls, line, lines):
        key, val = line.split("=", maxsplit=1)
        key = key.strip()
        val = val.strip()
        try:
            with suppress(ValueError):
                val = literal_eval(val)
        except SyntaxError:
            key, val = cls._split_line(line + next(lines), lines)
        return key, val

    @property
    def metadata_platform_name(self):
        """Platform name from the internal file metadata."""
        try:
            # Example: 'Terra' or 'Aqua'
            return self.metadata["INVENTORYMETADATA"]["ASSOCIATEDPLATFORMINSTRUMENTSENSOR"][
                "ASSOCIATEDPLATFORMINSTRUMENTSENSORCONTAINER"]["ASSOCIATEDPLATFORMSHORTNAME"]["VALUE"]
        except KeyError:
            return self._platform_name_from_filename()

    def _platform_name_from_filename(self):
        platform_indicator = self.filename_info["platform_indicator"]
        if platform_indicator in ("t", "O"):
            # t1.* or MOD*
            return "Terra"
        # a1.* or MYD*
        return "Aqua"

    @property
    def start_time(self):
        """Get the start time of the dataset."""
        try:
            date = (self.metadata["INVENTORYMETADATA"]["RANGEDATETIME"]["RANGEBEGINNINGDATE"]["VALUE"] + " " +
                    self.metadata["INVENTORYMETADATA"]["RANGEDATETIME"]["RANGEBEGINNINGTIME"]["VALUE"])
            return _modis_date(date)
        except KeyError:
            return self._start_time_from_filename()

    def _start_time_from_filename(self):
        return self.filename_info["start_time"]

    @property
    def end_time(self):
        """Get the end time of the dataset."""
        try:
            date = (self.metadata["INVENTORYMETADATA"]["RANGEDATETIME"]["RANGEENDINGDATE"]["VALUE"] + " " +
                    self.metadata["INVENTORYMETADATA"]["RANGEDATETIME"]["RANGEENDINGTIME"]["VALUE"])
            return _modis_date(date)
        except KeyError:
            return self.start_time

    def _read_dataset_in_file(self, dataset_name):
        if dataset_name not in self.sd.datasets():
            error_message = "Dataset name {} not included in available datasets {}".format(
                dataset_name, self.sd.datasets()
            )
            raise KeyError(error_message)

        dataset = self.sd.select(dataset_name)
        return dataset

    def load_dataset(self, dataset_name, is_category=False):
        """Load the dataset from HDF EOS file."""
        from satpy.readers.core.hdf4 import from_sds

        dataset = self._read_dataset_in_file(dataset_name)
        chunks = self._chunks_for_variable(dataset)
        dask_arr = from_sds(dataset, self.filename, chunks=chunks)
        dims = ("y", "x") if dask_arr.ndim == 2 else None
        data = xr.DataArray(dask_arr, dims=dims,
                            attrs=dataset.attributes())
        data = _scale_and_mask_data_array(data, is_category=is_category)

        return data

    def _chunks_for_variable(self, hdf_dataset):
        scan_length_250m = 40
        var_shape = hdf_dataset.info()[2]
        res_multiplier = self._get_res_multiplier(var_shape)
        num_nonyx_dims = len(var_shape) - 2
        return normalize_low_res_chunks(
            (1,) * num_nonyx_dims + ("auto", -1),
            var_shape,
            (1,) * num_nonyx_dims + (scan_length_250m, var_shape[-1]),
            (1,) * num_nonyx_dims + (res_multiplier, res_multiplier),
            np.float32,
        )

    @staticmethod
    def _get_res_multiplier(var_shape):
        num_columns_to_multiplier = {
            271: 20,  # 5km
            1354: 4,  # 1km
            2708: 2,  # 500m
            5416: 1,  # 250m
        }
        for max_columns, res_multiplier in num_columns_to_multiplier.items():
            if var_shape[-1] <= max_columns:
                return res_multiplier
        return 1

    def _add_satpy_metadata(self, data_id: DataID, data_arr: xr.DataArray):
        """Add metadata that is specific to Satpy."""
        new_attrs = {
            "platform_name": self.metadata_platform_name,
            "sensor": "modis",
        }

        res = data_id["resolution"]
        rps = self._resolution_to_rows_per_scan(res)
        new_attrs["rows_per_scan"] = rps

        data_arr.attrs.update(new_attrs)

    def _resolution_to_rows_per_scan(self, resolution: int) -> int:
        known_rps = {
            5000: 2,
            1000: 10,
            500: 20,
            250: 40,
        }
        return known_rps.get(resolution, 10)


class HDFEOSGeoReader(HDFEOSBaseFileReader):
    """Handler for the geographical datasets."""

    # list of geographical datasets handled by the georeader
    # mapping to the default variable name if not specified in YAML
    DATASET_NAMES = {
        "longitude": "Longitude",
        "latitude": "Latitude",
        "satellite_azimuth_angle": ("SensorAzimuth", "Sensor_Azimuth"),
        "satellite_zenith_angle": ("SensorZenith", "Sensor_Zenith"),
        "solar_azimuth_angle": ("SolarAzimuth", "SolarAzimuth"),
        "solar_zenith_angle": ("SolarZenith", "Solar_Zenith"),
        "water_present": "WaterPresent",
        "landsea_mask": "Land/SeaMask",
        "height": "Height",
        "range": "Range",
    }

    def __init__(self, filename, filename_info, filetype_info, **kwargs):
        """Initialize the geographical reader."""
        HDFEOSBaseFileReader.__init__(self, filename, filename_info, filetype_info, **kwargs)
        self._load_interpolated_lonlat_pair = cache(self._load_interpolated_lonlat_pair_uncached)
        self._load_interpolated_angle_pair = cache(self._load_interpolated_angle_pair_uncached)

    @staticmethod
    def is_geo_loadable_dataset(dataset_name: str) -> bool:
        """Determine if this dataset should be loaded as a Geo dataset."""
        return dataset_name in HDFEOSGeoReader.DATASET_NAMES

    @staticmethod
    def read_geo_resolution(metadata):
        """Parse metadata to find the geolocation resolution."""
        # level 1 files
        try:
            return HDFEOSGeoReader._geo_resolution_for_l1b(metadata)
        except KeyError:
            try:
                return HDFEOSGeoReader._geo_resolution_for_l2_l1b(metadata)
            except (AttributeError, KeyError):
                raise RuntimeError("Could not determine resolution from file metadata")

    @staticmethod
    def _geo_resolution_for_l1b(metadata):
        ds = metadata["INVENTORYMETADATA"]["COLLECTIONDESCRIPTIONCLASS"]["SHORTNAME"]["VALUE"]
        if ds.endswith("D03") or ds.endswith("HKM") or ds.endswith("QKM"):
            return 1000
        # 1km files have 5km geolocation usually
        return 5000

    @staticmethod
    def _geo_resolution_for_l2_l1b(metadata):
        # data files probably have this level 2 files
        # this does not work for L1B 1KM data files because they are listed
        # as 1KM data but the geo data inside is at 5km
        latitude_dim = metadata["SwathStructure"]["SWATH_1"]["DimensionMap"]["DimensionMap_2"]["GeoDimension"]
        resolution_regex = re.compile(r"(?P<resolution>\d+)(km|KM)")
        resolution_match = resolution_regex.search(latitude_dim)
        return int(resolution_match.group("resolution")) * 1000

    @property
    def geo_resolution(self):
        """Resolution of the geographical data retrieved in the metadata."""
        return self.read_geo_resolution(self.metadata)

    def _load_ds_by_name(self, ds_name):
        """Attempt loading using multiple common names."""
        var_names = self.DATASET_NAMES[ds_name]
        if isinstance(var_names, (list, tuple)):
            try:
                return self.load_dataset(var_names[0])
            except KeyError:
                return self.load_dataset(var_names[1])
        return self.load_dataset(var_names)

    def get_dataset(self, dataset_id: DataID, dataset_info: dict) -> xr.DataArray:
        """Get the geolocation dataset."""
        # Name of the dataset as it appears in the HDF EOS file
        in_file_dataset_name = dataset_info.get("file_key")
        # Name of the dataset in the YAML file
        dataset_name = dataset_id["name"]
        # Resolution asked
        resolution = dataset_id["resolution"]
        if in_file_dataset_name is not None:
            # if the YAML was configured with a specific name use that
            data = self.load_dataset(in_file_dataset_name)
        else:
            # otherwise use the default name for this variable
            data = self._load_ds_by_name(dataset_name)
        if resolution != self.geo_resolution:
            if in_file_dataset_name is not None:
                # they specified a custom variable name but
                # we don't know how to interpolate this yet
                raise NotImplementedError(
                    f"Interpolation for variable '{dataset_name}' is not "
                    "configured.")

            logger.debug(f"Loading and interpolating {dataset_name}")
            data = self.get_interpolated_dataset(dataset_name, resolution)

        for key in ("standard_name", "units"):
            if key in dataset_info:
                data.attrs[key] = dataset_info[key]
        self._add_satpy_metadata(dataset_id, data)

        return data

    def get_interpolated_dataset(self, dataset_name: str, resolution: int) -> xr.DataArray:
        """Load and interpolate datasets."""
        interp_pairs = {
            ("longitude", "latitude"): self._load_interpolated_lonlat_pair,
            ("satellite_azimuth_angle", "satellite_zenith_angle"): self._load_interpolated_angle_pair,
            ("solar_azimuth_angle", "solar_zenith_angle"): self._load_interpolated_angle_pair,
        }
        for ds_name_pair, interp_func in interp_pairs.items():
            try:
                pair_index = ds_name_pair.index(dataset_name)
            except ValueError:
                continue
            return interp_func(*ds_name_pair, resolution)[pair_index]
        raise ValueError(f"Dataset {dataset_name} can not be interpolated")

    def _load_interpolated_lonlat_pair_uncached(
            self,
            name1: str,
            name2: str,
            resolution: int
    ) -> tuple[xr.DataArray, xr.DataArray]:
        result1 = self._load_ds_by_name(name1)
        result2 = self._load_ds_by_name(name2)
        return self._interpolate_using_sza(result1, result2, resolution)

    def _load_interpolated_angle_pair_uncached(
            self,
            name1: str,
            name2: str,
            resolution: int
    ) -> tuple[xr.DataArray, xr.DataArray]:
        result1 = self._load_ds_by_name(name1)
        result2 = self._load_ds_by_name(name2) - 90
        interp_result1, interp_result2 = self._interpolate_using_sza(result1, result2, resolution)
        return interp_result1, interp_result2 + 90

    def _interpolate_using_sza(
            self,
            data1: xr.DataArray,
            data2: xr.DataArray,
            resolution: int
    ) -> tuple[xr.DataArray, xr.DataArray]:
        try:
            sensor_zenith = self._load_ds_by_name("satellite_zenith_angle")
        except KeyError:
            # no sensor zenith angle, do "simple" interpolation
            sensor_zenith = None
        return interpolate(
            data1, data2, sensor_zenith,
            self.geo_resolution, resolution
        )


def _scale_and_mask_data_array(data_arr: xr.DataArray, is_category: bool = False) -> xr.DataArray:
    """Unscale byte data and mask invalid/fill values.

    MODIS requires unscaling the in-file bytes in an unexpected way::

        data = (byte_value - add_offset) * scale_factor

    See the below L1B User's Guide Appendix C for more information:

    https://mcst.gsfc.nasa.gov/sites/default/files/file_attachments/M1054E_PUG_2017_0901_V6.2.2_Terra_V6.2.1_Aqua.pdf

    """
    scale_factor = data_arr.attrs.pop("scale_factor", None)
    add_offset = data_arr.attrs.pop("add_offset", None)
    # preserve _FillValue if category
    fill_value = data_arr.attrs.get("_FillValue", None) if is_category else data_arr.attrs.pop("_FillValue", None)
    noncat_dtype = data_arr.dtype.type if np.issubdtype(data_arr.dtype, np.floating) else np.float32
    dtype = data_arr.dtype if is_category else noncat_dtype
    new_data = da.map_blocks(
        _mapblocks_scale_and_mask,
        data_arr.data,
        dtype=dtype,
        meta=np.array((), dtype=dtype),
        name="scale_and_mask",
        scale_factor=scale_factor,
        add_offset=add_offset,
        fill_value=fill_value,
        is_category=is_category)
    data_arr = data_arr.copy()
    data_arr.data = new_data
    return data_arr


def _mapblocks_scale_and_mask(arr, scale_factor, add_offset, fill_value, is_category):
    good_mask, new_fill = _get_good_data_mask(arr, fill_value, is_category=is_category)
    # don't scale category products, even though scale_factor may equal 1
    # we still need to convert integers to floats
    if scale_factor is not None and not is_category:
        if add_offset is not None and add_offset != 0:
            arr = arr - np.float32(add_offset)
        arr = arr * np.float32(scale_factor)

    if good_mask is not None:
        arr = np.where(good_mask, arr, new_fill)
    return arr


def _get_good_data_mask(arr, fill_value, is_category=False):
    if fill_value is None:
        return None, None
    # preserve integer data types if possible
    if is_category and np.issubdtype(arr.dtype, np.integer):
        # no need to mask, the fill value is already what it needs to be
        return None, None
    fill_type = arr.dtype.type if np.issubdtype(arr.dtype, np.floating) else np.float32
    new_fill = fill_type(np.nan)
    good_mask = arr != fill_value
    return good_mask, new_fill
