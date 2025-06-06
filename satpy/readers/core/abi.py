#!/usr/bin/env python
# Copyright (c) 2016-2025 Satpy developers
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

"""Advance Baseline Imager reader base class for the Level 1b and l2+ reader."""

import datetime as dt
import logging
import math
from contextlib import suppress

import dask
import numpy as np
import xarray as xr
from pyresample import geometry

from satpy._compat import cached_property
from satpy.readers.core.file_handlers import BaseFileHandler
from satpy.readers.core.remote import open_file_or_filename
from satpy.utils import get_dask_chunk_size_in_bytes

logger = logging.getLogger(__name__)

PLATFORM_NAMES = {
    "g16": "GOES-16",
    "g17": "GOES-17",
    "g18": "GOES-18",
    "g19": "GOES-19",
    "goes16": "GOES-16",
    "goes17": "GOES-17",
    "goes18": "GOES-18",
}


class NC_ABI_BASE(BaseFileHandler):
    """Base reader for ABI L1B  L2+ NetCDF4 files."""

    def __init__(self, filename, filename_info, filetype_info):
        """Open the NetCDF file with xarray and prepare the Dataset for reading."""
        super(NC_ABI_BASE, self).__init__(filename, filename_info, filetype_info)

        platform_shortname = filename_info["platform_shortname"]
        self.platform_name = PLATFORM_NAMES.get(platform_shortname.lower())

        self.nlines = self.nc["y"].size
        self.ncols = self.nc["x"].size

        self.coords = {}

    @cached_property
    def nc(self):
        """Get the xarray dataset for this file."""
        chunk_bytes = self._chunk_bytes_for_resolution()
        with dask.config.set({"array.chunk-size": chunk_bytes}):
            f_obj = open_file_or_filename(self.filename)
            nc = xr.open_dataset(f_obj,
                                 decode_cf=True,
                                 mask_and_scale=False,
                                 chunks="auto")
        nc = self._rename_dims(nc)
        return nc

    def _chunk_bytes_for_resolution(self) -> int:
        """Get a best-guess optimal chunk size for resolution-based chunking.

        First a chunk size is chosen for the provided Dask setting `array.chunk-size`
        and then aligned with a hardcoded on-disk chunk size of 226. This is then
        adjusted to match the current resolution.

        This should result in 500 meter data having 4 times as many pixels per
        dask array chunk (2 in each dimension) as 1km data and 8 times as many
        as 2km data. As data is combined or upsampled geographically the arrays
        should not need to be rechunked. Care is taken to make sure that array
        chunks are aligned with on-disk file chunks at all resolutions, but at
        the cost of flexibility due to a hardcoded on-disk chunk size of 226
        elements per dimension.

        """
        num_high_res_elems_per_dim = math.sqrt(get_dask_chunk_size_in_bytes() / 4)  # 32-bit floats
        # assume on-disk chunk size of 226
        # this is true for all CSPP Geo GRB output (226 for all sectors) and full disk from other sources
        # 250 has been seen for AWS/CLASS CONUS, Mesoscale 1, and Mesoscale 2 files
        # we align this with 4 on-disk chunks at 500m, so it will be 2 on-disk chunks for 1km, and 1 for 2km
        high_res_elems_disk_aligned = round(max(num_high_res_elems_per_dim / (4 * 226), 1)) * (4 * 226)
        low_res_factor = int(self.filetype_info.get("resolution", 2000) // 500)
        res_elems_per_dim = int(high_res_elems_disk_aligned / low_res_factor)
        return (res_elems_per_dim ** 2) * 2  # 16-bit integers on disk

    @staticmethod
    def _rename_dims(nc):
        if "t" in nc.dims or "t" in nc.coords:
            nc = nc.rename({"t": "time"})
        if "goes_lat_lon_projection" in nc:
            with suppress(ValueError):
                nc = nc.rename({"lon": "x", "lat": "y"})
        return nc

    @property
    def sensor(self):
        """Get sensor name for current file handler."""
        return "abi"

    def __getitem__(self, item):
        """Wrap `self.nc[item]` for better floating point precision.

        Some datasets use a 32-bit float scaling factor like the 'x' and 'y'
        variables which causes inaccurate unscaled data values. This method
        forces the scale factor to a 64-bit float first.
        """
        data = self.nc[item]
        attrs = data.attrs

        data = self._adjust_data(data, item)

        data.attrs = attrs

        data = self._adjust_coords(data, item)

        return data

    def _adjust_data(self, data, item):
        """Adjust data with typing, scaling and filling."""
        factor = data.attrs.get("scale_factor", 1)
        offset = data.attrs.get("add_offset", 0)
        fill = data.attrs.get("_FillValue")
        unsigned = data.attrs.get("_Unsigned", None)

        def is_int(val):
            return np.issubdtype(val.dtype, np.integer) if hasattr(val, "dtype") else isinstance(val, int)

        # Ref. GOESR PUG-L1B-vol3, section 5.0.2 Unsigned Integer Processing
        if unsigned is not None and unsigned.lower() == "true":
            # cast the data from int to uint
            data = data.astype("u%s" % data.dtype.itemsize)

            if fill is not None:
                fill = fill.astype("u%s" % fill.dtype.itemsize)
        if fill is not None:
            # Some backends (h5netcdf) may return attributes as shape (1,)
            # arrays rather than shape () scalars, which according to the netcdf
            # documentation at <URL:https://www.unidata.ucar.edu
            # /software/netcdf/docs/netcdf_data_set_components.html#attributes>
            # is correct.
            if np.ndim(fill) > 0:
                fill = fill.item()
            if is_int(data) and is_int(factor) and is_int(offset):
                new_fill = fill
            else:
                new_fill = np.float32(np.nan)
            data = data.where(data != fill, new_fill)
        if factor != 1 and item in ("x", "y"):
            # be more precise with x/y coordinates
            # see get_area_def for more information
            data = data * np.round(float(factor), 6) + np.round(float(offset), 6)
        elif factor != 1:
            data = data * np.float32(factor) + np.float32(offset)
        return data

    def _adjust_coords(self, data, item):
        """Handle coordinates (and recursive fun)."""
        new_coords = {}
        # 'time' dimension causes issues in other processing
        # 'x_image' and 'y_image' are confusing to some users and unnecessary
        # 'x' and 'y' will be overwritten by base class AreaDefinition
        for coord_name in ("x_image", "y_image", "time", "x", "y"):
            if coord_name in data.coords:
                data = data.drop_vars(coord_name)
        if item in data.coords:
            self.coords[item] = data
        for coord_name in data.coords.keys():
            if coord_name not in self.coords:
                self.coords[coord_name] = self[coord_name]
            new_coords[coord_name] = self.coords[coord_name]
        data.coords.update(new_coords)
        return data

    def get_dataset(self, key, info):
        """Load a dataset."""
        raise NotImplementedError("Reader {} has not implemented get_dataset".format(self.name))

    def get_area_def(self, key):
        """Get the area definition of the data at hand."""
        if "goes_imager_projection" in self.nc:
            return self._get_areadef_fixedgrid(key)
        if "goes_lat_lon_projection" in self.nc:
            return self._get_areadef_latlon(key)
        raise ValueError("Unsupported projection found in the dataset")

    def _get_areadef_latlon(self, key):
        """Get the area definition of the data at hand."""
        projection = self.nc["goes_lat_lon_projection"]

        a = projection.attrs["semi_major_axis"]
        b = projection.attrs["semi_minor_axis"]
        fi = projection.attrs["inverse_flattening"]
        pm = projection.attrs["longitude_of_prime_meridian"]

        proj_ext = self.nc["geospatial_lat_lon_extent"]

        w_lon = proj_ext.attrs["geospatial_westbound_longitude"]
        e_lon = proj_ext.attrs["geospatial_eastbound_longitude"]
        n_lat = proj_ext.attrs["geospatial_northbound_latitude"]
        s_lat = proj_ext.attrs["geospatial_southbound_latitude"]

        lat_0 = proj_ext.attrs["geospatial_lat_center"]
        lon_0 = proj_ext.attrs["geospatial_lon_center"]

        area_extent = (w_lon, s_lat, e_lon, n_lat)
        proj_dict = {"proj": "latlong",
                     "lon_0": float(lon_0),
                     "lat_0": float(lat_0),
                     "a": float(a),
                     "b": float(b),
                     "fi": float(fi),
                     "pm": float(pm)}

        ll_area_def = geometry.AreaDefinition(
            self.nc.attrs.get("orbital_slot", "abi_geos"),
            self.nc.attrs.get("spatial_resolution", "ABI file area"),
            "abi_latlon",
            proj_dict,
            self.ncols,
            self.nlines,
            np.asarray(area_extent))

        return ll_area_def

    def _get_areadef_fixedgrid(self, key):
        """Get the area definition of the data at hand.

        Note this method takes special care to round and cast numbers to new
        data types so that the area definitions for different resolutions
        (different bands) should be equal. Without the special rounding in
        `__getitem__` and this method the area extents can be 0 to 1.0 meters
        off depending on how the calculations are done.

        """
        projection = self.nc["goes_imager_projection"]
        a = projection.attrs["semi_major_axis"]
        b = projection.attrs["semi_minor_axis"]
        h = projection.attrs["perspective_point_height"]

        lon_0 = projection.attrs["longitude_of_projection_origin"]
        sweep_axis = projection.attrs["sweep_angle_axis"][0]

        # compute x and y extents in m
        h = np.float64(h)
        x = self["x"]
        y = self["y"]
        x_l = x[0].values
        x_r = x[-1].values
        y_l = y[-1].values
        y_u = y[0].values
        x_half = (x_r - x_l) / (self.ncols - 1) / 2.
        y_half = (y_u - y_l) / (self.nlines - 1) / 2.
        area_extent = (x_l - x_half, y_l - y_half, x_r + x_half, y_u + y_half)
        area_extent = tuple(np.round(h * val, 6) for val in area_extent)

        proj_dict = {"proj": "geos",
                     "lon_0": float(lon_0),
                     "a": float(a),
                     "b": float(b),
                     "h": h,
                     "units": "m",
                     "sweep": sweep_axis}

        fg_area_def = geometry.AreaDefinition(
            self.nc.attrs.get("orbital_slot", "abi_geos"),
            self.nc.attrs.get("spatial_resolution", "ABI file area"),
            "abi_fixed_grid",
            proj_dict,
            self.ncols,
            self.nlines,
            np.asarray(area_extent))

        return fg_area_def

    @property
    def start_time(self):
        """Start time of the current file's observations."""
        return dt.datetime.strptime(self.nc.attrs["time_coverage_start"], "%Y-%m-%dT%H:%M:%S.%fZ")

    @property
    def end_time(self):
        """End time of the current file's observations."""
        return dt.datetime.strptime(self.nc.attrs["time_coverage_end"], "%Y-%m-%dT%H:%M:%S.%fZ")

    def spatial_resolution_to_number(self):
        """Convert the 'spatial_resolution' global attribute to meters."""
        res = self.nc.attrs["spatial_resolution"].split(" ")[0]
        if res.endswith("km"):
            res = int(float(res[:-2]) * 1000)
        elif res.endswith("m"):
            res = int(res[:-1])
        else:
            raise ValueError("Unexpected 'spatial_resolution' attribute '{}'".format(res))
        return res
