#!/usr/bin/env python
# -*- coding: utf-8 -*-
# Copyright (c) 2017-2019 Satpy developers
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

"""Interface to MTG-FCI L1c NetCDF files.

This module defines the :class:`FCIL1cNCFileHandler` file handler, to
be used for reading Meteosat Third Generation (MTG) Flexible Combined
Imager (FCI) Level-1c data.  FCI flies
on the MTG Imager (MTG-I) series of satellites, with the first satellite (MTG-I1)
launched on the 13th of December 2022.
For more information about FCI, see `EUMETSAT`_.

To download data to be used with this reader, see `MTG data store guide`_.
For the Product User Guide (PUG) of the FCI L1c data, see `PUG`_.

.. note::

    This reader currently supports Full Disk High Spectral Resolution Imagery
    (FDHSI), High Spatial Resolution Fast Imagery (HRFI) data in full-disc ("FD") or in RSS ("Q4") scanning mode.
    In addition, it also supports the L1C format for the African dissemination ("AF"), where each file
    contains the masked full-dic of a single channel see `AF PUG`_.
    Experimental support for special scans, e.g. with coverage "xx", is also given.

    This reader supports data from both IDPF-I and IQT-I processing facilities.


.. note::

    If the user provides a list of both FDHSI and HRFI files from the same repeat cycle to the Satpy ``Scene``,
    Satpy will automatically read the channels from the source with the finest resolution,
    i.e. from the HRFI files for the vis_06, nir_22, ir_38, and ir_105 channels.
    If needed, the desired resolution can be explicitly requested using e.g.:
    ``scn.load(['vis_06'], resolution=1000)``.


Geolocation is based on information from the data files.  It uses:

    * From the shape of the data variable ``data/<channel>/measured/effective_radiance``,
      start and end line columns of current swath.
    * From the data variable ``data/<channel>/measured/x``, the x-coordinates
      for the grid, in radians (azimuth angle positive towards West).
    * From the data variable ``data/<channel>/measured/y``, the y-coordinates
      for the grid, in radians (elevation angle positive towards North).
    * From the attribute ``semi_major_axis`` on the data variable
      ``data/mtg_geos_projection``, the Earth equatorial radius
    * From the attribute ``inverse_flattening`` on the same data variable, the
      (inverse) flattening of the ellipsoid
    * From the attribute ``perspective_point_height`` on the same data
      variable, the geostationary altitude in the normalised geostationary
      projection
    * From the attribute ``longitude_of_projection_origin`` on the same
      data variable, the longitude of the projection origin
    * From the attribute ``sweep_angle_axis`` on the same, the sweep angle
      axis, see https://proj.org/operations/projections/geos.html

From the pixel centre angles in radians and the geostationary altitude, the
extremities of the lower left and upper right corners are calculated in units
of arc length in m.  This extent along with the number of columns and rows, the
sweep angle axis, and a dictionary with equatorial radius, polar radius,
geostationary altitude, and longitude of projection origin, are passed on to
``pyresample.geometry.AreaDefinition``, which then uses proj4 for the actual
geolocation calculations.


The reading routine supports channel data in counts, radiances, and (depending
on channel) brightness temperatures or reflectances. The brightness temperature and reflectance calculation is based on
the formulas indicated in `PUG`_.
Radiance datasets are returned in units of radiance per unit wavenumber (mW m-2 sr-1 (cm-1)-1). Radiances can be
converted to units of radiance per unit wavelength (W m-2 um-1 sr-1) by multiplying with the
`radiance_unit_conversion_coefficient` dataset attribute.

For each channel, it also supports a number of auxiliary datasets, such as the pixel quality,
the index map and the related geometric and acquisition parameters: time,
subsatellite latitude, subsatellite longitude, platform altitude, subsolar latitude, subsolar longitude,
earth-sun distance, sun-satellite distance,  swath number, and swath direction.

All auxiliary data can be obtained by prepending the channel name such as
``"vis_04_pixel_quality"``.

.. warning::
    The API for the direct reading of pixel quality is temporary and likely to
    change.  Currently, for each channel, the pixel quality is available by
    ``<chan>_pixel_quality``.  In the future, they will likely all be called
    ``pixel_quality`` and disambiguated by a to-be-decided property in the
    `DataID`.

.. note::

    For reading compressed data, a decompression library is
    needed. Either install the FCIDECOMP library (see `PUG`_), or the
    ``hdf5plugin`` package with::

        pip install hdf5plugin

    or::

        conda install hdf5plugin -c conda-forge

    If you use ``hdf5plugin``, make sure to add the line ``import hdf5plugin``
    at the top of your script.

.. _AF PUG: https://user.eumetsat.int/resources/user-guides/mtg-africa-data-service-guide  # noqa: E501
.. _PUG: https://user.eumetsat.int/resources/user-guides/mtg-fci-level-1c-data-guide  # noqa: E501
.. _EUMETSAT: https://user.eumetsat.int/resources/user-guides/mtg-fci-level-1c-data-guide  # noqa: E501
.. _MTG data store guide: https://user.eumetsat.int/resources/user-guides/data-store-mtg-data-access-guide  # noqa: E501
"""

from __future__ import absolute_import, division, print_function, unicode_literals

import datetime as dt
import logging
from functools import cached_property

import dask.array as da
import numpy as np
import xarray as xr
from netCDF4 import default_fillvals
from pyorbital.astronomy import sun_earth_distance_correction
from pyresample import geometry

import satpy
from satpy.readers.core._geos_area import get_geos_area_naming
from satpy.readers.core.eum import get_service_mode
from satpy.readers.core.fci import platform_name_translate
from satpy.readers.core.netcdf import NetCDF4FsspecFileHandler

logger = logging.getLogger(__name__)

# dict containing all available auxiliary data parameters to be read using the index map. Keys are the
# parameter name and values are the paths to the variable inside the netcdf
AUX_DATA = {
    "subsatellite_latitude": "state/platform/subsatellite_latitude",
    "subsatellite_longitude": "state/platform/subsatellite_longitude",
    "platform_altitude": "state/platform/platform_altitude",
    "subsolar_latitude": "state/celestial/subsolar_latitude",
    "subsolar_longitude": "state/celestial/subsolar_longitude",
    "earth_sun_distance": "state/celestial/earth_sun_distance",
    "sun_satellite_distance": "state/celestial/sun_satellite_distance",
    "time": "time",
    "swath_number": "data/swath_number",
    "swath_direction": "data/swath_direction",
}

HIGH_RES_GRID_INFO = {"fci_l1c_hrfi": {"grid_type": "500m",
                                       "grid_width": 22272},
                      "fci_l1c_fdhsi": {"grid_type": "1km",
                                        "grid_width": 11136},
                      }
LOW_RES_GRID_INFO = {"fci_l1c_hrfi": {"grid_type": "1km",
                                      "grid_width": 11136},
                     "fci_l1c_fdhsi": {"grid_type": "2km",
                                       "grid_width": 5568},
                     }


def _get_aux_data_name_from_dsname(dsname):
    aux_data_name = [key for key in AUX_DATA.keys() if key in dsname]
    if len(aux_data_name) > 0:
        return aux_data_name[0]

    return None


def _get_channel_name_from_dsname(dsname):
    # FIXME: replace by .removesuffix after we drop support for Python < 3.9
    if dsname.endswith("_pixel_quality"):
        channel_name = dsname[:-len("_pixel_quality")]
    elif dsname.endswith("_index_map"):
        channel_name = dsname[:-len("_index_map")]
    elif _get_aux_data_name_from_dsname(dsname) is not None:
        channel_name = dsname[:-len(_get_aux_data_name_from_dsname(dsname)) - 1]
    else:
        channel_name = dsname

    return channel_name


class FCIL1cNCFileHandler(NetCDF4FsspecFileHandler):
    """Class implementing the MTG FCI L1c Filehandler.

    This class implements the Meteosat Third Generation (MTG) Flexible
    Combined Imager (FCI) Level-1c NetCDF reader.
    It is designed to be used through the :class:`satpy.Scene <satpy.scene.Scene>`
    class using the :mod:`Scene.load <satpy.scene.Scene.load>` method with the reader
    ``"fci_l1c_nc"``.

    """
    def __init__(self, filename, filename_info, filetype_info,
                 clip_negative_radiances=None, **kwargs):
        """Initialize file handler."""
        super().__init__(filename, filename_info,
                         filetype_info,
                         cache_var_size=0,
                         cache_handle=True)
        logger.debug("Reading: {}".format(self.filename))
        logger.debug("Start: {}".format(self.start_time))
        logger.debug("End: {}".format(self.end_time))

        if self.filename_info["coverage"] == "Q4":
            # change the chunk number so that padding gets activated correctly for Q4, which corresponds to the upper
            # quarter of the disc
            self.filename_info["count_in_repeat_cycle"] += 28

        if self.filename_info["coverage"] == "AF":
            # change number of chunk from 0 to 1 so that the padding is not activated (chunk 1 is present and only 1
            # chunk is expected), as the African dissemination products come in one file per full disk.
            self.filename_info["count_in_repeat_cycle"] = 1

        if self.filename_info["coverage"] == "xx":
            # add one to the chunk numbering to activate the padding mechanism for special scans (expected to have less
            # than 40 chunks, but still starting with 1)
            self.filename_info["count_in_repeat_cycle"] += 1

        if self.filename_info["facility_or_tool"] == "IQTI":
            self.is_iqt = True
        else:
            self.is_iqt = False

        if clip_negative_radiances is None:
            clip_negative_radiances = satpy.config.get("readers.clip_negative_radiances")
        self.clip_negative_radiances = clip_negative_radiances
        self._cache = {}

    @property
    def rc_period_min(self):
        """Get nominal repeat cycle duration."""
        if "Q4" in self.filename_info["coverage"]:
            return 2.5
        elif self.filename_info["coverage"] in ["FD", "AF"]:
            return 10
        else:
            logger.debug(f"Coverage \"{self.filename_info['coverage']}\" not recognised. "
                         f"Using observation times for nominal times.")
            return None


    @property
    def nominal_start_time(self):
        """Get nominal start time."""
        if self.rc_period_min is None:
            return self.filename_info["start_time"]
        else:
            rc_date = self.observation_start_time.replace(hour=0, minute=0, second=0, microsecond=0)
            return rc_date + dt.timedelta(
                minutes=(self.filename_info["repeat_cycle_in_day"] - 1) * self.rc_period_min)

    @property
    def nominal_end_time(self):
        """Get nominal end time."""
        if self.rc_period_min is None:
            return self.filename_info["end_time"]
        else:
            return self.nominal_start_time + dt.timedelta(minutes=self.rc_period_min)

    @property
    def observation_start_time(self):
        """Get observation start time."""
        return self.filename_info["start_time"]

    @property
    def observation_end_time(self):
        """Get observation end time."""
        return self.filename_info["end_time"]

    @property
    def start_time(self):
        """Get start time."""
        return self.nominal_start_time

    @property
    def end_time(self):
        """Get end time."""
        return self.nominal_end_time

    def get_channel_measured_group_path(self, channel):
        """Get the channel's measured group path."""
        if self.filetype_info["file_type"] == "fci_l1c_hrfi":
            channel += "_hr"
        measured_group_path = "data/{}/measured".format(channel)

        return measured_group_path

    def get_segment_position_info(self):
        """Get information about the size and the position of the segment inside the final image array.

        As the final array is composed by stacking segments vertically, the position of a segment
        inside the array is defined by the numbers of the start (lowest) and end (highest) row of the segment.
        The row numbering is assumed to start with 1.
        This info is used in the GEOVariableSegmentYAMLReader to compute optimal segment sizes for missing segments.

        Note: in the FCI terminology, a segment is actually called "chunk". To avoid confusion with the dask concept
        of chunk, and to be consistent with SEVIRI, we opt to use the word segment.

        Note: This function is not used for the African data as it contains only one segment.
        """
        file_type = self.filetype_info["file_type"]
        vis_06_measured_path = self.get_channel_measured_group_path("vis_06")
        ir_105_measured_path = self.get_channel_measured_group_path("ir_105")
        segment_position_info = {
            HIGH_RES_GRID_INFO[file_type]["grid_type"]: {
                "start_position_row": self.get_and_cache_npxr(vis_06_measured_path + "/start_position_row").item(),
                "end_position_row": self.get_and_cache_npxr(vis_06_measured_path + "/end_position_row").item(),
                "segment_height": self.get_and_cache_npxr(vis_06_measured_path + "/end_position_row").item() -
                                  self.get_and_cache_npxr(vis_06_measured_path + "/start_position_row").item() + 1,
                "grid_width": HIGH_RES_GRID_INFO[file_type]["grid_width"]
            },
            LOW_RES_GRID_INFO[file_type]["grid_type"]: {
                "start_position_row": self.get_and_cache_npxr(ir_105_measured_path + "/start_position_row").item(),
                "end_position_row": self.get_and_cache_npxr(ir_105_measured_path + "/end_position_row").item(),
                "segment_height": self.get_and_cache_npxr(ir_105_measured_path + "/end_position_row").item() -
                                  self.get_and_cache_npxr(ir_105_measured_path + "/start_position_row").item() + 1,
                "grid_width": LOW_RES_GRID_INFO[file_type]["grid_width"]
            }
        }
        return segment_position_info

    def get_dataset(self, key, info=None):
        """Load a dataset."""
        logger.debug("Reading {} from {}".format(key["name"], self.filename))
        if "pixel_quality" in key["name"]:
            return self._get_dataset_quality(key["name"])
        elif "index_map" in key["name"]:
            return self._get_dataset_index_map(key["name"])
        elif _get_aux_data_name_from_dsname(key["name"]) is not None:
            return self._get_dataset_aux_data(key["name"])
        elif any(lb in key["name"] for lb in {"vis_", "ir_", "nir_", "wv_"}):
            return self._get_dataset_measurand(key, info=info)
        else:
            raise ValueError("Unknown dataset key, not a channel, quality or auxiliary data: "
                             f"{key['name']:s}")

    def _get_dataset_measurand(self, key, info=None):
        """Load dataset corresponding to channel measurement.

        Load a dataset when the key refers to a measurand, whether uncalibrated
        (counts) or calibrated in terms of brightness temperature, radiance, or
        reflectance.
        """
        # Get the dataset
        # Get metadata for given dataset
        measured = self.get_channel_measured_group_path(key["name"])
        data = self[measured + "/effective_radiance"]

        attrs = dict(data.attrs).copy()
        info = info.copy()
        data = _ensure_dataarray(data)

        fv = attrs.pop(
            "FillValue",
            default_fillvals.get(data.dtype.str[1:], np.float32(np.nan)))
        vr = attrs.get("valid_range", [np.float32(-np.inf), np.float32(np.inf)])
        if key["calibration"] == "counts":
            attrs["_FillValue"] = fv
            nfv = data.dtype.type(fv)
        else:
            nfv = np.float32(np.nan)
        data = data.where((data >= vr[0]) & (data <= vr[1]), nfv)

        res = self.calibrate(data, key)

        # pre-calibration units no longer apply
        attrs.pop("units")

        # For each channel, the effective_radiance contains in the
        # "ancillary_variables" attribute the value "pixel_quality".  In
        # FileYAMLReader._load_ancillary_variables, satpy will try to load
        # "pixel_quality" but is lacking the context from what group to load
        # it: in the FCI format, each channel group (data/<channel>/measured) has
        # its own data variable 'pixel_quality'.
        # Until we can have multiple pixel_quality variables defined (for
        # example, with https://github.com/pytroll/satpy/pull/1088), rewrite
        # the ancillary variable to include the channel. See also
        # https://github.com/pytroll/satpy/issues/1171.
        if "pixel_quality" in attrs["ancillary_variables"]:
            attrs["ancillary_variables"] = attrs["ancillary_variables"].replace(
                "pixel_quality", key["name"] + "_pixel_quality")
        else:
            raise ValueError(
                "Unexpected value for attribute ancillary_variables, "
                "which the FCI file handler intends to rewrite (see "
                "https://github.com/pytroll/satpy/issues/1171 for why). "
                f"Expected 'pixel_quality', got {attrs['ancillary_variables']:s}")

        res.attrs.update(key.to_dict())
        res.attrs.update(info)
        res.attrs.update(attrs)

        res.attrs["platform_name"] = platform_name_translate.get(
            self["attr/platform"], self["attr/platform"])

        # remove unpacking parameters for calibrated data
        if key["calibration"] in ["brightness_temperature", "reflectance", "radiance"]:
            res.attrs.pop("add_offset")
            res.attrs.pop("warm_add_offset")
            res.attrs.pop("scale_factor")
            res.attrs.pop("warm_scale_factor")
            res.attrs.pop("valid_range")

        # remove attributes from original file which don't apply anymore
        res.attrs.pop("long_name")
        # Add time_parameter attributes
        res.attrs["time_parameters"] = {
            "nominal_start_time": self.nominal_start_time,
            "nominal_end_time": self.nominal_end_time,
            "observation_start_time": self.observation_start_time,
            "observation_end_time": self.observation_end_time,
        }
        res.attrs.update(self.orbital_param)

        return res

    def get_iqt_parameters_lon_lat_alt(self):
        """Compute the orbital parameters for IQT data.

        Compute satellite_actual_longitude, satellite_actual_latitude, satellite_actual_altitude.
        """
        actual_subsat_lon = float(self.get_and_cache_npxr("data/mtg_geos_projection/attr/"
                                                          "longitude_of_projection_origin"))
        actual_subsat_lat = 0.0
        actual_sat_alt = float(self.get_and_cache_npxr("data/mtg_geos_projection/attr/perspective_point_height"))
        logger.info("For IQT data, the following parameter is hardcoded:"
                    f" satellite_actual_latitude = {actual_subsat_lat}. "
                    "The following parameters are taken from the projection dictionary: "
                    f"satellite_actual_longitude = {actual_subsat_lon}, "
                    f"satellite_actual_altitude = {actual_sat_alt}")
        return actual_subsat_lon, actual_subsat_lat, actual_sat_alt

    def get_parameters_lon_lat_alt(self):
        """Compute the orbital parameters.

        Compute satellite_actual_longitude, satellite_actual_latitude, satellite_actual_altitude.
        """
        actual_subsat_lon = float(np.nanmean(self._get_aux_data_lut_vector("subsatellite_longitude")))
        actual_subsat_lat = float(np.nanmean(self._get_aux_data_lut_vector("subsatellite_latitude")))
        actual_sat_alt = float(np.nanmean(self._get_aux_data_lut_vector("platform_altitude")))
        return actual_subsat_lon, actual_subsat_lat, actual_sat_alt

    @cached_property
    def orbital_param(self):
        """Compute the orbital parameters for the current segment."""
        if self.is_iqt:
            actual_subsat_lon, actual_subsat_lat, actual_sat_alt = self.get_iqt_parameters_lon_lat_alt()
        else:
            actual_subsat_lon, actual_subsat_lat, actual_sat_alt = self.get_parameters_lon_lat_alt()
        # The "try" is a temporary part of the code as long as the AF data are not fixed
        try:
            nominal_and_proj_subsat_lon = float(
                self.get_and_cache_npxr("data/mtg_geos_projection/attr/longitude_of_projection_origin"))
        except ValueError:
            nominal_and_proj_subsat_lon = 0.0
        nominal_and_proj_subsat_lat = 0.0
        nominal_and_proj_sat_alt = float(
            self.get_and_cache_npxr("data/mtg_geos_projection/attr/perspective_point_height"))

        orb_param_dict = {
            "orbital_parameters": {
                "satellite_actual_longitude": actual_subsat_lon,
                "satellite_actual_latitude": actual_subsat_lat,
                "satellite_actual_altitude": actual_sat_alt,
                "satellite_nominal_longitude": nominal_and_proj_subsat_lon,
                "satellite_nominal_latitude": nominal_and_proj_subsat_lat,
                "satellite_nominal_altitude": nominal_and_proj_sat_alt,
                "projection_longitude": nominal_and_proj_subsat_lon,
                "projection_latitude": nominal_and_proj_subsat_lat,
                "projection_altitude": nominal_and_proj_sat_alt,
            }}

        return orb_param_dict

    def _get_dataset_quality(self, dsname):
        """Load a quality field for an FCI channel."""
        grp_path = self.get_channel_measured_group_path(_get_channel_name_from_dsname(dsname))
        dv_path = grp_path + "/pixel_quality"
        data = self[dv_path]
        return data

    def _get_dataset_index_map(self, dsname):
        """Load the index map for an FCI channel."""
        grp_path = self.get_channel_measured_group_path(_get_channel_name_from_dsname(dsname))
        dv_path = grp_path + "/index_map"
        data = self[dv_path]

        data = data.where(data != data.attrs.get("_FillValue", 65535))
        return data

    def _get_aux_data_lut_vector(self, aux_data_name):
        """Load the lut vector of an auxiliary variable."""
        lut = self.get_and_cache_npxr(AUX_DATA[aux_data_name])
        lut = _ensure_dataarray(lut)
        fv = default_fillvals.get(lut.dtype.str[1:], np.nan)
        lut = lut.where(lut != fv)

        return lut

    @staticmethod
    def _getitem(block, lut):
        return lut[block.astype("uint16")]

    def _get_dataset_aux_data(self, dsname):
        """Get the auxiliary data arrays using the index map."""
        # get index map
        index_map = self._get_dataset_index_map(_get_channel_name_from_dsname(dsname))
        # subtract minimum of index variable (index_offset)
        index_map -= np.min(self.get_and_cache_npxr("index"))

        # get lut values from 1-d vector variable
        lut = self._get_aux_data_lut_vector(_get_aux_data_name_from_dsname(dsname))
        # assign lut values based on index map indices
        aux = index_map.data.map_blocks(self._getitem, lut.data, dtype=lut.data.dtype)
        aux = xr.DataArray(aux, dims=index_map.dims, attrs=index_map.attrs, coords=index_map.coords)

        # filter out out-of-disk values
        aux = aux.where(index_map >= 0)

        return aux

    def calc_area_extent(self, key):
        """Calculate area extent for a dataset."""
        # if a user requests a pixel quality or index map before the channel data, the
        # yaml-reader will ask the area extent of the pixel quality/index map field,
        # which will ultimately end up here
        channel_name = _get_channel_name_from_dsname(key["name"])
        # Get metadata for given dataset
        measured = self.get_channel_measured_group_path(channel_name)
        # Get start/end line and column of loaded swath.
        nlines, ncols = self[measured + "/effective_radiance/shape"]

        logger.debug("Channel {} resolution: {}".format(channel_name, ncols))
        logger.debug("Row/Cols: {} / {}".format(nlines, ncols))

        # Calculate full globe line extent
        h = float(self.get_and_cache_npxr("data/mtg_geos_projection/attr/perspective_point_height"))

        extents = {}
        for coord in "xy":
            coord_radian = self.get_and_cache_npxr(measured + "/{:s}".format(coord))

            coord_radian_num = coord_radian[:] * coord_radian.attrs["scale_factor"] + coord_radian.attrs["add_offset"]

            # FCI defines pixels by centroids (see PUG), while pyresample
            # defines corners as lower left corner of lower left pixel, upper right corner of upper right pixel
            # (see https://pyresample.readthedocs.io/en/latest/geo_def.html).
            # Therefore, half a pixel (i.e. half scale factor) needs to be added in each direction.

            # The grid origin is in the South-West corner.
            # Note that the azimuth angle (x) is defined as positive towards West (see PUG - Level 1c Reference Grid)
            # The elevation angle (y) is defined as positive towards North as per usual convention. Therefore:
            # The values of x go from positive (West) to negative (East) and the scale factor of x is negative.
            # The values of y go from negative (South) to positive (North) and the scale factor of y is positive.

            # South-West corner (x positive, y negative)
            first_coord_radian = coord_radian_num[0] - coord_radian.attrs["scale_factor"] / 2
            # North-East corner (x negative, y positive)
            last_coord_radian = coord_radian_num[-1] + coord_radian.attrs["scale_factor"] / 2

            # convert to arc length in m
            first_coord = first_coord_radian * h  # arc length in m
            last_coord = last_coord_radian * h

            # the .item() call is needed with the h5netcdf backend, see
            # https://github.com/pytroll/satpy/issues/972#issuecomment-558191583
            # but we need to compute it first if this is dask
            try:
                first_coord = first_coord.compute()
                last_coord = last_coord.compute()
            except AttributeError:  # not a dask.array
                pass

            extents[coord] = (first_coord.item(), last_coord.item())

        # For the final extents, take into account that the image is upside down (lower line is North), and that
        # East is defined as positive azimuth in Proj, so we need to multiply by -1 the azimuth extents.

        # lower left x: west-ward extent: first coord of x, multiplied by -1 to account for azimuth orientation
        # lower left y: north-ward extent: last coord of y
        # upper right x: east-ward extent: last coord of x, multiplied by -1 to account for azimuth orientation
        # upper right y: south-ward extent: first coord of y
        area_extent = (-extents["x"][0], extents["y"][1], -extents["x"][1], extents["y"][0])

        return area_extent, nlines, ncols

    def get_area_def(self, key):
        """Calculate on-fly area definition for a dataset in geos-projection."""
        # assumption: channels with same resolution should have same area
        # cache results to improve performance
        if key["resolution"] in self._cache:
            return self._cache[key["resolution"]]

        a = float(self.get_and_cache_npxr("data/mtg_geos_projection/attr/semi_major_axis"))
        h = float(self.get_and_cache_npxr("data/mtg_geos_projection/attr/perspective_point_height"))
        rf = float(self.get_and_cache_npxr("data/mtg_geos_projection/attr/inverse_flattening"))
        # The "try" is a temporary part of the code as long as the AF data are not modified
        try:
            lon_0 = float(self.get_and_cache_npxr("data/mtg_geos_projection/attr/longitude_of_projection_origin"))
        except ValueError:
            lon_0 = 0.0
        sweep = str(self.get_and_cache_npxr("data/mtg_geos_projection/attr/sweep_angle_axis"))

        area_extent, nlines, ncols = self.calc_area_extent(key)
        logger.debug("Calculated area extent: {}"
                     .format("".join(str(area_extent))))

        # use a (semi-major axis) and rf (reverse flattening) to define ellipsoid as recommended by EUM (see PUG)
        proj_dict = {"a": a,
                     "lon_0": lon_0,
                     "h": h,
                     "rf": rf,
                     "proj": "geos",
                     "units": "m",
                     "sweep": sweep}

        area_naming_input_dict = {"platform_name": "mtg",
                                  "instrument_name": "fci",
                                  "resolution": int(key["resolution"])
                                  }
        area_naming = get_geos_area_naming({**area_naming_input_dict,
                                            **get_service_mode("fci", lon_0)})

        area = geometry.AreaDefinition(
            area_naming["area_id"],
            area_naming["description"],
            "",
            proj_dict,
            ncols,
            nlines,
            area_extent)

        self._cache[key["resolution"]] = area
        return area

    def calibrate(self, data, key):
        """Calibrate data."""
        if key["calibration"] in ["brightness_temperature", "reflectance", "radiance"]:
            data = self.calibrate_counts_to_physical_quantity(data, key)
        elif key["calibration"] != "counts":
            logger.error(
                "Received unknown calibration key.  Expected "
                "'brightness_temperature', 'reflectance', 'radiance' or 'counts', got "
                + key["calibration"] + ".")

        return data

    def calibrate_counts_to_physical_quantity(self, data, key):
        """Calibrate counts to radiances, brightness temperatures, or reflectances."""
        # counts to radiance scaling

        data = self.calibrate_counts_to_rad(data, key)

        if key["calibration"] == "brightness_temperature":
            data = self.calibrate_rad_to_bt(data, key)
        elif key["calibration"] == "reflectance":
            data = self.calibrate_rad_to_refl(data, key)

        return data

    def calibrate_counts_to_rad(self, data, key):
        """Calibrate counts to radiances."""
        if self.clip_negative_radiances:
            data = self._clipneg(data)
        if key["name"] == "ir_38":
            data = xr.where(((2 ** 12 - 1 < data) & (data <= 2 ** 13 - 1)),
                            (data * data.attrs.get("warm_scale_factor", 1) +
                             data.attrs.get("warm_add_offset", 0)),
                            (data * data.attrs.get("scale_factor", 1) +
                             data.attrs.get("add_offset", 0))
                            )
        else:
            data = (data * data.attrs.get("scale_factor", 1) +
                    data.attrs.get("add_offset", 0))

        measured = self.get_channel_measured_group_path(key["name"])
        data.attrs.update({"radiance_unit_conversion_coefficient":
                               self.get_and_cache_npxr(measured + "/radiance_unit_conversion_coefficient")})
        return data

    @staticmethod
    def _clipneg(data):
        """Clip counts to avoid negative radiances."""
        lo = -data.attrs.get("add_offset", 0) // data.attrs.get("scale_factor", 1) + 1
        return data.where((~data.notnull())|(data>=lo), lo)

    def calibrate_rad_to_bt(self, radiance, key):
        """IR channel calibration."""
        # using the method from PUG section Converting from Effective Radiance to Brightness Temperature for IR Channels
        measured = self.get_channel_measured_group_path(key["name"])

        vc = self.get_and_cache_npxr(measured + "/radiance_to_bt_conversion_coefficient_wavenumber").astype(np.float32)

        a = self.get_and_cache_npxr(measured + "/radiance_to_bt_conversion_coefficient_a").astype(np.float32)
        b = self.get_and_cache_npxr(measured + "/radiance_to_bt_conversion_coefficient_b").astype(np.float32)

        c1 = self.get_and_cache_npxr(measured + "/radiance_to_bt_conversion_constant_c1").astype(np.float32)
        c2 = self.get_and_cache_npxr(measured + "/radiance_to_bt_conversion_constant_c2").astype(np.float32)

        for v in (vc, a, b, c1, c2):
            if v == v.attrs.get("FillValue",
                                default_fillvals.get(v.dtype.str[1:])):
                logger.error(
                    "{:s} set to fill value, cannot produce "
                    "brightness temperatures for {:s}.".format(
                        v.attrs.get("long_name",
                                    "at least one necessary coefficient"),
                        measured))
                return radiance * np.float32(np.nan)

        nom = c2 * vc
        denom = a * np.log(1 + (c1 * vc ** np.float32(3.)) / radiance)

        res = nom / denom - b / a

        return res

    def calibrate_rad_to_refl(self, radiance, key):
        """VIS channel calibration."""
        measured = self.get_channel_measured_group_path(key["name"])

        cesi = self.get_and_cache_npxr(measured + "/channel_effective_solar_irradiance").astype(np.float32)

        if cesi == cesi.attrs.get(
                "FillValue", default_fillvals.get(cesi.dtype.str[1:])):
            logger.error(
                "channel effective solar irradiance set to fill value, "
                "cannot produce reflectance for {:s}.".format(measured))
            return radiance * np.float32(np.nan)
        sun_earth_distance = self._compute_sun_earth_distance
        res = 100 * radiance * np.float32(np.pi) * np.float32(sun_earth_distance) ** np.float32(2) / cesi
        return res

    @cached_property
    def _compute_sun_earth_distance(self) -> float:
        """Compute the sun_earth_distance."""
        if self.is_iqt:
            middle_time_diff = (self.observation_end_time - self.observation_start_time) / 2
            utc_date = self.observation_start_time + middle_time_diff
            sun_earth_distance = sun_earth_distance_correction(utc_date)
            logger.info(f"The value sun_earth_distance is set to {sun_earth_distance} AU.")
        else:
            sun_earth_distance = np.nanmean(
                self._get_aux_data_lut_vector("earth_sun_distance")) / 149597870.7  # [AU]
        return sun_earth_distance


def _ensure_dataarray(arr):
    if not isinstance(arr, xr.DataArray):
        attrs = dict(arr.attrs).copy()
        arr = xr.DataArray(da.from_array(arr), dims=arr.dimensions, attrs=attrs, name=arr.name)
    return arr
