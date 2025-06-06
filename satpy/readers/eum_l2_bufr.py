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

"""SEVIRI L2 BUFR format reader.

References:
    EUMETSAT Product Navigator
    https://navigator.eumetsat.int/

"""

import datetime as dt
import logging
import os

import dask.array as da
import numpy as np
import xarray as xr

from satpy.area import get_area_def
from satpy.readers.core._geos_area import get_geos_area_naming
from satpy.readers.core.eum import get_service_mode, recarray2dict
from satpy.readers.core.file_handlers import BaseFileHandler
from satpy.readers.core.seviri import mpef_product_header
from satpy.utils import get_legacy_chunk_size

try:
    import eccodes as ec
except ImportError:
    raise ImportError(
        "Missing eccodes-python and/or eccodes C-library installation. Use conda to install eccodes")

logger = logging.getLogger("EumetsatL2Bufr")

CHUNK_SIZE = get_legacy_chunk_size()

SSP_DEFAULT = 0.0
BUFR_FILL_VALUE = -1.e+100

data_center_dict = {55: {"ssp": "E0415", "name": "MSG1"}, 56: {"ssp": "E0455", "name": "MSG2"},
                    57: {"ssp": "E0095", "name": "MSG3"}, 70: {"ssp": "E0000", "name": "MSG4"},
                    71: {"ssp": "E0000", "name": "MTGi1"}}

# Sensor resolution (pixel size in m) used to deermine product segment sizes
resolution_dict = {"fci": 2000, "seviri": 3000}

# List of variables that are now returned by eccodes as array, but that we want as single value
deprecate_to_single_value = ["satelliteIdentifier"]


class EumetsatL2BufrFileHandler(BaseFileHandler):
    """File handler for EUMETSAT Central Facility SEVIRI and FCI L2 BUFR products.

    **Loading data with AreaDefinition**

    By providing the `with_area_definition` as True in the `reader_kwargs`, the dataset is loaded with
    an AreaDefinition using a standardized AreaDefinition in areas.yaml. By default, the dataset will
    be loaded with a SwathDefinition, i.e. similar to how the data are stored in the BUFR file:

        scene = satpy.Scene(filenames,
                            reader="seviri_l2_bufr",
                            reader_kwargs={"with_area_definition": False})

    **Defining dataset recticifation longitude**

    The BUFR data were originally extracted from a rectified two-dimensional grid with a given central longitude
    (typically the sub-satellite point). This information is not available in the file itself nor the filename (for
    files from the EUMETSAT archive). Also, it cannot be reliably derived from all datasets themselves. Hence, the
    rectification longitude can be defined by the user by providing `rectification_longitude` in the `reader_kwargs`:

        scene = satpy.Scene(filenames,
                            reader="seviri_l2_bufr",
                            reader_kwargs={"rectification_longitude": 0.0})

    If not done, default values applicable to the operational grids of the respective SEVIRI instruments will be used.
    """

    def __init__(self, filename, filename_info, filetype_info, with_area_definition=False,
                 rectification_longitude="default", **kwargs):
        """Initialise the file handler for EUMETSAT SEVIRI and FCI L2 BUFR data."""
        super(EumetsatL2BufrFileHandler, self).__init__(filename,
                                                        filename_info,
                                                        filetype_info)

        # Set this environment variable to get consistent array sizes from eccodes. This fixes the cases
        # where all values in the expected array are the same (in particular fill values) which causes
        # eccodes to encode them and return them as a single value
        os.environ["ECCODES_BUFR_MULTI_ELEMENT_CONSTANT_ARRAYS"] = "1"

        if ("server" in filename_info):
            # EUMETSAT Offline Bufr product
            self.bufr_header = self._read_mpef_header()
        else:
            # Product was retrieved from the EUMETSAT Data Center
            attr = self.get_attributes(["typicalDate", "typicalTime", "satelliteIdentifier"])
            timeStr = attr["typicalDate"]+attr["typicalTime"]
            sc_id = int(attr["satelliteIdentifier"])

            self.bufr_header = {}
            self.bufr_header["NominalTime"] = dt.datetime.strptime(timeStr, "%Y%m%d%H%M%S")
            self.bufr_header["SpacecraftName"] = data_center_dict[sc_id]["name"]
            self.bufr_header["RectificationLongitude"] = data_center_dict[sc_id]["ssp"]

        if rectification_longitude != "default":
            self.bufr_header["RectificationLongitude"] = f"E{int(rectification_longitude * 10):04d}"

        self.filetype = filetype_info["file_type"]
        self.with_adef = with_area_definition

    def __del__(self):
        """Delete the instance and environment variable."""
        try:
            del os.environ["ECCODES_BUFR_MULTI_ELEMENT_CONSTANT_ARRAYS"]
        except KeyError:
            pass

    @property
    def start_time(self):
        """Return the repeat cycle start time."""
        return self.bufr_header["NominalTime"]

    @property
    def platform_name(self):
        """Return spacecraft name."""
        if self.bufr_header["SpacecraftName"].isdigit():
            # self._read_mpef_header will return the SpacecraftName as a string with an integer, representing the
            # Meteosat series number (1-11). This is cnverted to the corresponding MSG ID.
            return f'MSG{int(self.bufr_header["SpacecraftName"])-7}'
        else:
            return self.bufr_header["SpacecraftName"]

    @property
    def sensor_name(self):
        """Return instrument name."""
        if self.platform_name.startswith("MSG"):
            return "seviri"
        elif self.platform_name.startswith("MTG"):
            return "fci"

    @property
    def ssp_lon(self):
        """Return subsatellite point longitude."""
        ssp_lon = self.bufr_header["RectificationLongitude"]
        return float(ssp_lon[1:])/10.

    def get_area_def(self, key):
        """Return the area definition."""
        try:
            return self._area_def
        except AttributeError:
            raise NotImplementedError

    def _read_mpef_header(self):
        """Read MPEF header."""
        hdr = np.fromfile(self.filename, mpef_product_header, 1)
        return recarray2dict(hdr)

    def get_attributes(self, keys):
        """Get BUFR attributes."""
        # This function is inefficient as it is looping through the entire
        # file to get a list of attributes. It causes a problem though if you break
        # from the file early - dont know why but investigating - fix later.
        fh = open(self.filename, "rb")

        # Initialize output
        attrs = dict()

        while True:
            # Get handle for message
            bufr = ec.codes_bufr_new_from_file(fh)
            if bufr is None:
                break
            ec.codes_set(bufr, "unpack", 1)
            for k in keys:
                try:
                    if k in deprecate_to_single_value:
                        # With ECCODES_BUFR_MULTI_ELEMENT_CONSTANT_ARRAYS set to 1 all values, including scalars, are
                        # returned as arrays. Hence, we extract the single value here.
                        value = ec.codes_get_array(bufr, k)[0]
                    else:
                        value = ec.codes_get(bufr, k)
                    attrs[k] = value

                except BaseException:
                    attrs[k] = None
                    logging.warning(f"Failed to read key {k} from message")

            ec.codes_release(bufr)

        fh.close()
        return attrs

    def get_array(self, key):
        """Get all data from file for the given BUFR key."""
        with open(self.filename, "rb") as fh:
            msgCount = 0
            while True:
                bufr = ec.codes_bufr_new_from_file(fh)
                if bufr is None:
                    break

                ec.codes_set(bufr, "unpack", 1)

                if not ec.codes_is_defined(bufr, key):
                    logging.warning(f"Key: {key} does not exist in BUFR file")
                    return None

                # if is the first message initialise our final array
                if (msgCount == 0):
                    arr = da.from_array(ec.codes_get_array(bufr, key, float), chunks=CHUNK_SIZE)

                else:
                    tmpArr = da.from_array(ec.codes_get_array(bufr, key, float), chunks=CHUNK_SIZE)
                    arr = da.concatenate((arr, tmpArr))

                msgCount = msgCount+1
                ec.codes_release(bufr)

        if arr.size == 1:
            arr = arr[0]

        return arr

    def get_dataset(self, dataset_id, dataset_info):
        """Create dataset.

        Load data from BUFR file using the BUFR key in dataset_info
        and create the dataset with or without an AreaDefinition.

        """
        arr = self.get_array(dataset_info["key"])

        if self.with_adef and "resolution" in dataset_id:
            xarr = self.get_dataset_with_area_def(arr, dataset_id)
            # Coordinates are not relevant when returning data with an AreaDefinition
            if "coordinates" in dataset_info.keys():
                del dataset_info["coordinates"]
        else:
            if self.with_adef:
                logging.warning("Trying to use `with_area_definition=True` for a dataset without resolution. "
                                "This is not supported and the keyword will be ignored.")
            xarr = xr.DataArray(arr, dims=["y"])

        if "fill_value" in dataset_info:
            xarr = xarr.where(xarr != dataset_info["fill_value"])

        self._add_attributes(xarr, dataset_info)
        return xarr

    def get_dataset_with_area_def(self, arr, dataset_id):
        """Get dataset with an AreaDefinition."""
        if dataset_id["name"] in ["latitude", "longitude"]:
            self.__setattr__(dataset_id["name"], arr)
            xarr = xr.DataArray(arr, dims=["y"])
        else:
            lons_1d, lats_1d, data_1d = da.compute(self.longitude, self.latitude, arr)
            self._area_def = self._construct_area_def(dataset_id)
            icol, irow = self._area_def.get_array_indices_from_lonlat(lons_1d, lats_1d)

            data_2d = np.empty(self._area_def.shape)
            data_2d[:] = np.nan
            data_2d[irow.compressed(), icol.compressed()] = data_1d[~irow.mask]

            xarr = xr.DataArray(da.from_array(data_2d, CHUNK_SIZE), dims=("y", "x"))

            ntotal = len(icol)
            nvalid = len(icol.compressed())
            if nvalid < ntotal:
                logging.warning(f"{ntotal-nvalid} out of {ntotal} data points could not be put on "
                                f"the grid {self._area_def.area_id}.")

        return xarr

    def _construct_area_def(self, dataset_id):
        """Construct a standardized AreaDefinition based on satellite, instrument, resolution and sub-satellite point.

        Returns:
            A pyresample AreaDefinition object containing the area definition.

        """
        area_naming_input_dict = {"platform_name": self.platform_name[:3].lower(),
                                  "instrument_name": self.sensor_name,
                                  "resolution": dataset_id["resolution"],
                                  }

        area_naming = get_geos_area_naming({**area_naming_input_dict,
                                            **get_service_mode(self.sensor_name, self.ssp_lon)})

        # Datasets with a segment size of 3 pixels extend outside the original SEVIRI 3km grid (with 1238 x 1238
        # segments a 3 pixels). Hence, we need to use corresponding area defintions in areas.yaml
        if self.sensor_name=="seviri" and round(dataset_id["resolution"])==9001:
            area_naming["area_id"] += "_ext"
            area_naming["description"] += " (extended outside original 3km grid)"

        # Construct AreaDefinition from standardized area definition in areas.yaml.
        stand_area_def = get_area_def(area_naming["area_id"])

        return stand_area_def

    def _add_attributes(self, xarr, dataset_info):
        """Add dataset attributes to xarray."""
        xarr.attrs["sensor"] = self.sensor_name.upper()
        xarr.attrs["platform_name"] = self.platform_name
        xarr.attrs["ssp_lon"] = self.ssp_lon
        if ("resolution" not in dataset_info) or (dataset_info["resolution"] is None):
            xarr.attrs["seg_size"] = None
            xarr.attrs["resolution"] = None
        else:
            xarr.attrs["seg_size"] = round(dataset_info["resolution"]/resolution_dict[self.sensor_name])

        xarr.attrs.update(dataset_info)
