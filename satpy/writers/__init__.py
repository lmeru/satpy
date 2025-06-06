# Copyright (c) 2015-2023 Satpy developers
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
"""Shared objects of the various writer classes.

For now, this includes enhancement configuration utilities.
"""
from __future__ import annotations

import os
import warnings
from typing import Optional

import dask
import dask.array as da
import numpy as np
import xarray as xr
import yaml
from trollimage.xrimage import XRImage
from trollsift import parser
from yaml import UnsafeLoader

from satpy._config import config_search_paths, get_entry_points_config_dirs, glob_config
from satpy.area import get_area_def
from satpy.aux_download import DataDownloadMixin
from satpy.enhancements.enhancer import Enhancer
from satpy.plugin_base import Plugin
from satpy.utils import get_legacy_chunk_size, get_logger

LOG = get_logger(__name__)
CHUNK_SIZE = get_legacy_chunk_size()


def read_writer_config(config_files, loader=UnsafeLoader):
    """Read the writer `config_files` and return the info extracted."""
    conf = {}
    LOG.debug("Reading %s", str(config_files))
    for config_file in config_files:
        with open(config_file) as fd:
            conf.update(yaml.load(fd.read(), Loader=loader))

    try:
        writer_info = conf["writer"]
    except KeyError:
        raise KeyError(
            "Malformed config file {}: missing writer 'writer'".format(
                config_files))
    writer_info["config_files"] = config_files
    return writer_info


def load_writer_configs(writer_configs, **writer_kwargs):
    """Load the writer from the provided `writer_configs`."""
    try:
        writer_info = read_writer_config(writer_configs)
        writer_class = writer_info["writer"]
    except (ValueError, KeyError, yaml.YAMLError):
        raise ValueError("Invalid writer configs: "
                         "'{}'".format(writer_configs))
    init_kwargs, kwargs = writer_class.separate_init_kwargs(writer_kwargs)
    writer = writer_class(config_files=writer_configs,
                          **init_kwargs)
    return writer, kwargs


def load_writer(writer, **writer_kwargs):
    """Find and load writer `writer` in the available configuration files."""
    config_fn = writer + ".yaml" if "." not in writer else writer
    config_files = config_search_paths(os.path.join("writers", config_fn))
    writer_kwargs.setdefault("config_files", config_files)
    if not writer_kwargs["config_files"]:
        raise ValueError("Unknown writer '{}'".format(writer))

    try:
        return load_writer_configs(writer_kwargs["config_files"],
                                   **writer_kwargs)
    except ValueError:
        raise ValueError("Writer '{}' does not exist or could not be "
                         "loaded".format(writer))


def configs_for_writer(writer=None):
    """Generate writer configuration files for one or more writers.

    Args:
        writer (Optional[str]): Yield configs only for this writer

    Returns: Generator of lists of configuration files

    """
    if writer is not None:
        if not isinstance(writer, (list, tuple)):
            writer = [writer]
        # given a config filename or writer name
        config_files = [w if w.endswith(".yaml") else w + ".yaml" for w in writer]
    else:
        paths = get_entry_points_config_dirs("satpy.writers")
        writer_configs = glob_config(os.path.join("writers", "*.yaml"), search_dirs=paths)
        config_files = set(writer_configs)

    for config_file in config_files:
        config_basename = os.path.basename(config_file)
        paths = get_entry_points_config_dirs("satpy.writers")
        writer_configs = config_search_paths(
            os.path.join("writers", config_basename),
            search_dirs=paths,
        )

        if not writer_configs:
            LOG.warning("No writer configs found for '%s'", writer)
            continue

        yield writer_configs


def available_writers(as_dict=False):
    """Available writers based on current configuration.

    Args:
        as_dict (bool): Optionally return writer information as a dictionary.
                        Default: False

    Returns: List of available writer names. If `as_dict` is `True` then
             a list of dictionaries including additionally writer information
             is returned.

    """
    writers = []
    for writer_configs in configs_for_writer():
        try:
            writer_info = read_writer_config(writer_configs)
        except (KeyError, IOError, yaml.YAMLError):
            LOG.warning("Could not import writer config from: %s", writer_configs)
            LOG.debug("Error loading YAML", exc_info=True)
            continue
        writers.append(writer_info if as_dict else writer_info["name"])
    return writers


def _determine_mode(dataset):
    if "mode" in dataset.attrs:
        return dataset.attrs["mode"]

    if dataset.ndim == 2:
        return "L"
    if dataset.shape[0] == 2:
        return "LA"
    if dataset.shape[0] == 3:
        return "RGB"
    if dataset.shape[0] == 4:
        return "RGBA"
    raise RuntimeError("Can't determine 'mode' of dataset: %s" %
                       str(dataset))


def _burn_overlay(img, image_metadata, area, cw_, overlays):
    """Burn the overlay in the image array."""
    del image_metadata
    cw_.add_overlay_from_dict(overlays, area, background=img)
    return img


def add_overlay(orig_img, area, coast_dir, color=None, width=None, resolution=None,
                level_coast=None, level_borders=None, fill_value=None,
                grid=None, overlays=None):
    """Add coastline, political borders and grid(graticules) to image.

    Uses ``color`` for feature colors where ``color`` is a 3-element tuple
    of integers between 0 and 255 representing (R, G, B).

    .. warning::

        This function currently loses the data mask (alpha band).

    ``resolution`` is chosen automatically if None (default),
    otherwise it should be one of:

    +-----+-------------------------+---------+
    | 'f' | Full resolution         | 0.04 km |
    +-----+-------------------------+---------+
    | 'h' | High resolution         | 0.2 km  |
    +-----+-------------------------+---------+
    | 'i' | Intermediate resolution | 1.0 km  |
    +-----+-------------------------+---------+
    | 'l' | Low resolution          | 5.0 km  |
    +-----+-------------------------+---------+
    | 'c' | Crude resolution        | 25  km  |
    +-----+-------------------------+---------+

    ``grid`` is a dictionary with key values as documented in detail in pycoast

    eg. overlay={'grid': {'major_lonlat': (10, 10),
                          'write_text': False,
                          'outline': (224, 224, 224),
                          'width': 0.5}}

    Here major_lonlat is plotted every 10 deg for both longitude and latitude,
    no labels for the grid lines are plotted, the color used for the grid lines
    is light gray, and the width of the gratucules is 0.5 pixels.

    For grid if aggdraw is used, font option is mandatory, if not
    ``write_text`` is set to False::

        font = aggdraw.Font('black', '/usr/share/fonts/truetype/msttcorefonts/Arial.ttf',
                            opacity=127, size=16)

    """
    if area is None:
        raise ValueError("Area of image is None, can't add overlay.")

    from pycoast import ContourWriterAGG
    if isinstance(area, str):
        area = get_area_def(area)
    LOG.info("Add coastlines and political borders to image.")

    old_args = [color, width, resolution, grid, level_coast, level_borders]
    if any(arg is not None for arg in old_args):
        warnings.warn(
            "'color', 'width', 'resolution', 'grid', 'level_coast', 'level_borders'"
            " arguments will be deprecated soon. Please use 'overlays' instead.",
            DeprecationWarning,
            stacklevel=2
        )
    if hasattr(orig_img, "convert"):
        # image must be in RGB space to work with pycoast/pydecorate
        res_mode = ("RGBA" if orig_img.final_mode(fill_value).endswith("A") else "RGB")
        orig_img = orig_img.convert(res_mode)
    elif not orig_img.mode.startswith("RGB"):
        raise RuntimeError("'trollimage' 1.6+ required to support adding "
                           "overlays/decorations to non-RGB data.")

    if overlays is None:
        overlays = _create_overlays_dict(color, width, grid, level_coast, level_borders)

    cw_ = ContourWriterAGG(coast_dir)
    new_image = orig_img.apply_pil(_burn_overlay, res_mode,
                                   None, {"fill_value": fill_value},
                                   (area, cw_, overlays), None)
    return new_image


def _create_overlays_dict(color, width, grid, level_coast, level_borders):
    """Fill in the overlays dict."""
    overlays = dict()
    # fill with sensible defaults
    general_params = {"outline": color or (0, 0, 0),
                      "width": width or 0.5}
    for key, val in general_params.items():
        if val is not None:
            overlays.setdefault("coasts", {}).setdefault(key, val)
            overlays.setdefault("borders", {}).setdefault(key, val)
    if level_coast is None:
        level_coast = 1
    overlays.setdefault("coasts", {}).setdefault("level", level_coast)
    if level_borders is None:
        level_borders = 1
    overlays.setdefault("borders", {}).setdefault("level", level_borders)
    if grid is not None:
        if "major_lonlat" in grid and grid["major_lonlat"]:
            major_lonlat = grid.pop("major_lonlat")
            minor_lonlat = grid.pop("minor_lonlat", major_lonlat)
            grid.update({"Dlonlat": major_lonlat, "dlonlat": minor_lonlat})
        for key, val in grid.items():
            overlays.setdefault("grid", {}).setdefault(key, val)
    return overlays


def add_text(orig, dc, img, text):
    """Add text to an image using the pydecorate package.

    All the features of pydecorate's ``add_text`` are available.
    See documentation of :doc:`pydecorate:index` for more info.

    """
    LOG.info("Add text to image.")

    dc.add_text(**text)

    arr = da.from_array(np.array(img) / 255.0, chunks=CHUNK_SIZE)

    new_data = xr.DataArray(arr, dims=["y", "x", "bands"],
                            coords={"y": orig.data.coords["y"],
                                    "x": orig.data.coords["x"],
                                    "bands": list(img.mode)},
                            attrs=orig.data.attrs)
    return XRImage(new_data)


def add_logo(orig, dc, img, logo):
    """Add logos or other images to an image using the pydecorate package.

    All the features of pydecorate's ``add_logo`` are available.
    See documentation of :doc:`pydecorate:index` for more info.

    """
    LOG.info("Add logo to image.")

    dc.add_logo(**logo)

    arr = da.from_array(np.array(img) / 255.0, chunks=CHUNK_SIZE)

    new_data = xr.DataArray(arr, dims=["y", "x", "bands"],
                            coords={"y": orig.data.coords["y"],
                                    "x": orig.data.coords["x"],
                                    "bands": list(img.mode)},
                            attrs=orig.data.attrs)
    return XRImage(new_data)


def add_scale(orig, dc, img, scale):
    """Add scale to an image using the pydecorate package.

    All the features of pydecorate's ``add_scale`` are available.
    See documentation of :doc:`pydecorate:index` for more info.

    """
    LOG.info("Add scale to image.")

    dc.add_scale(**scale)

    arr = da.from_array(np.array(img) / 255.0, chunks=CHUNK_SIZE)

    new_data = xr.DataArray(arr, dims=["y", "x", "bands"],
                            coords={"y": orig.data.coords["y"],
                                    "x": orig.data.coords["x"],
                                    "bands": list(img.mode)},
                            attrs=orig.data.attrs)
    return XRImage(new_data)


def add_decorate(orig, fill_value=None, **decorate):
    """Decorate an image with text and/or logos/images.

    This call adds text/logos in order as given in the input to keep the
    alignment features available in pydecorate.

    An example of the decorate config::

        decorate = {
            'decorate': [
                {'logo': {'logo_path': <path to a logo>, 'height': 143, 'bg': 'white', 'bg_opacity': 255}},
                {'text': {'txt': start_time_txt,
                          'align': {'top_bottom': 'bottom', 'left_right': 'right'},
                          'font': <path to ttf font>,
                          'font_size': 22,
                          'height': 30,
                          'bg': 'black',
                          'bg_opacity': 255,
                          'line': 'white'}}
            ]
        }

    Any numbers of text/logo in any order can be added to the decorate list,
    but the order of the list is kept as described above.

    Note that a feature given in one element, eg. bg (which is the background color)
    will also apply on the next elements  unless a new value is given.

    align is a special keyword telling where in the image to start adding features, top_bottom is either top or bottom
    and left_right is either left or right.
    """
    LOG.info("Decorate image.")

    # Need to create this here to possible keep the alignment
    # when adding text and/or logo with pydecorate
    if hasattr(orig, "convert"):
        # image must be in RGB space to work with pycoast/pydecorate
        orig = orig.convert("RGBA" if orig.mode.endswith("A") else "RGB")
    elif not orig.mode.startswith("RGB"):
        raise RuntimeError("'trollimage' 1.6+ required to support adding "
                           "overlays/decorations to non-RGB data.")
    img_orig = orig.pil_image(fill_value=fill_value)
    from pydecorate import DecoratorAGG
    dc = DecoratorAGG(img_orig)

    # decorate need to be a list to maintain the alignment
    # as ordered in the list
    img = orig
    if "decorate" in decorate:
        for dec in decorate["decorate"]:
            if "logo" in dec:
                img = add_logo(img, dc, img_orig, logo=dec["logo"])
            elif "text" in dec:
                img = add_text(img, dc, img_orig, text=dec["text"])
            elif "scale" in dec:
                img = add_scale(img, dc, img_orig, scale=dec["scale"])
    return img


def get_enhanced_image(dataset, enhance=None, overlay=None, decorate=None,
                       fill_value=None):
    """Get an enhanced version of `dataset` as an :class:`~trollimage.xrimage.XRImage` instance.

    Args:
        dataset (xarray.DataArray): Data to be enhanced and converted to an image.
        enhance (bool or satpy.enhancements.enhancer.Enhancer): Whether to automatically enhance
            data to be more visually useful and to fit inside the file
            format being saved to. By default, this will default to using
            the enhancement configuration files found using the default
            :class:`~satpy.enhancements.enhancer.Enhancer` class. This can be set to
            `False` so that no enhancments are performed. This can also
            be an instance of the :class:`~satpy.enhancements.enhancer.Enhancer` class
            if further custom enhancement is needed.
        overlay (dict): Options for image overlays. See :func:`add_overlay`
            for available options.
        decorate (dict): Options for decorating the image. See
            :func:`add_decorate` for available options.
        fill_value (int or float): Value to use when pixels are masked or
            invalid. Default of `None` means to create an alpha channel.
            See :meth:`~trollimage.xrimage.XRImage.finalize` for more
            details. Only used when adding overlays or decorations. Otherwise
            it is up to the caller to "finalize" the image before using it
            except if calling ``img.show()`` or providing the image to
            a writer as these will finalize the image.
    """
    if enhance is False:
        # no enhancement
        enhancer = None
    elif enhance is None or enhance is True:
        # default enhancement
        enhancer = Enhancer()
    else:
        # custom enhancer
        enhancer = enhance

    # Create an image for enhancement
    img = to_image(dataset)

    if enhancer is None or enhancer.enhancement_tree is None:
        LOG.debug("No enhancement being applied to dataset")
    else:
        if dataset.attrs.get("sensor", None):
            enhancer.add_sensor_enhancements(dataset.attrs["sensor"])

        enhancer.apply(img, **dataset.attrs)

    if overlay is not None:
        img = add_overlay(img, dataset.attrs["area"], fill_value=fill_value, **overlay)

    if decorate is not None:
        img = add_decorate(img, fill_value=fill_value, **decorate)

    return img


def show(dataset, **kwargs):
    """Display the dataset as an image."""
    img = get_enhanced_image(dataset.squeeze(), **kwargs)
    img.show()
    return img


def to_image(dataset):
    """Convert ``dataset`` into a :class:`~trollimage.xrimage.XRImage` instance.

    Convert the ``dataset`` into an instance of the
    :class:`~trollimage.xrimage.XRImage` class.  This function makes no other
    changes.  To get an enhanced image, possibly with overlays and decoration,
    see :func:`~get_enhanced_image`.

    Args:
        dataset (xarray.DataArray): Data to be converted to an image.

    Returns:
        Instance of :class:`~trollimage.xrimage.XRImage`.

    """
    dataset = dataset.squeeze()
    if dataset.ndim < 2:
        raise ValueError("Need at least a 2D array to make an image.")
    return XRImage(dataset)


def split_results(results):
    """Split results.

    Get sources, targets and delayed objects to separate lists from a list of
    results collected from (multiple) writer(s).
    """
    from dask.delayed import Delayed

    def flatten(results):
        out = []
        if isinstance(results, (list, tuple)):
            for itm in results:
                out.extend(flatten(itm))
            return out
        return [results]

    sources = []
    targets = []
    delayeds = []

    for res in flatten(results):
        if isinstance(res, da.Array):
            sources.append(res)
        elif isinstance(res, Delayed):
            delayeds.append(res)
        else:
            targets.append(res)
    return sources, targets, delayeds


def group_results_by_output_file(sources, targets):
    """Group results by output file.

    For writers that return sources and targets for ``compute=False``, split
    the results by output file.

    When not only the data but also GeoTIFF tags are dask arrays, then
    ``save_datasets(..., compute=False)``` returns a tuple of flat lists,
    where the second list consists of a mixture of ``RIOTag`` and ``RIODataset``
    objects (from trollimage).  In some cases, we may want to get a seperate
    delayed object for each file; for example, if we want to add a wrapper to do
    something with the file as soon as it's finished.  This function unflattens
    the flat lists into a list of (src, target) tuples.

    For example, to close files as soon as computation is completed::

        >>> @dask.delayed
        >>> def closer(obj, targs):
        ...     for targ in targs:
        ...         targ.close()
        ...     return obj
        >>> (srcs, targs) = sc.save_datasets(writer="ninjogeotiff", compute=False, **ninjo_tags)
        >>> for (src, targ) in group_results_by_output_file(srcs, targs):
        ...     delayed_store = da.store(src, targ, compute=False)
        ...     wrapped_store = closer(delayed_store, targ)
        ...     wrapped.append(wrapped_store)
        >>> compute_writer_results(wrapped)

    In the wrapper you can do other useful tasks, such as writing a log message
    or moving files to a different directory.

    .. warning::

        Adding a callback may impact runtime and RAM.  The pattern or cause is
        unclear.  Tests with FCI data show that for resampling with high RAM
        use (from around 15 GB), runtime increases when a callback is added.
        Tests with ABI or low RAM consumption rather show a decrease in runtime.
        More information, see `these GitHub comments
        <https://github.com/pytroll/satpy/pull/2281#issuecomment-1324910253>`_
        Users who find out more are encouraged to contact the Satpy developers
        with clues.

    Args:
        sources: List of sources (typically dask.array) as returned by
            :meth:`Scene.save_datasets <satpy.scene.Scene.save_datasets>`.
        targets: List of targets (should be ``RIODataset`` or ``RIOTag``) as
            returned by :meth:`Scene.save_datasets <satpy.scene.Scene.save_datasets>`.

    Returns:
        List of ``Tuple(List[sources], List[targets])`` with a length equal to
        the number of output files planned to be written by
        :meth:`Scene.save_datasets <satpy.scene.Scene.save_datasets>`.
    """
    ofs = {}
    for (src, targ) in zip(sources, targets):
        fn = targ.rfile.path
        if fn not in ofs:
            ofs[fn] = ([], [])
        ofs[fn][0].append(src)
        ofs[fn][1].append(targ)
    return list(ofs.values())


def compute_writer_results(results):
    """Compute all the given dask graphs `results` so that the files are saved.

    Args:
        results (Iterable): Iterable of dask graphs resulting from calls to
                            `scn.save_datasets(..., compute=False)`
    """
    if not results:
        return

    sources, targets, delayeds = split_results(results)

    # one or more writers have targets that we need to close in the future
    if targets:
        delayeds.append(da.store(sources, targets, compute=False))

    if delayeds:
        # replace Delayed's graph optimization function with the Array function
        # since a Delayed object here is only from the writer but the rest of
        # the tasks are dask array operations we want to fully optimize all
        # array operations. At the time of writing Array optimizations seem to
        # include the optimizations done for Delayed objects alone.
        with dask.config.set(delayed_optimization=dask.config.get("array_optimize", da.optimize)):
            da.compute(delayeds)

    if targets:
        for target in targets:
            if hasattr(target, "close"):
                target.close()


class Writer(Plugin, DataDownloadMixin):
    """Base Writer class for all other writers.

    A minimal writer subclass should implement the `save_dataset` method.
    """

    def __init__(self, name=None, filename=None, base_dir=None, **kwargs):
        """Initialize the writer object.

        Args:
            name (str): A name for this writer for log and error messages.
                If this writer is configured in a YAML file its name should
                match the name of the YAML file. Writer names may also appear
                in output file attributes.
            filename (str): Filename to save data to. This filename can and
                should specify certain python string formatting fields to
                differentiate between data written to the files. Any
                attributes provided by the ``.attrs`` of a DataArray object
                may be included. Format and conversion specifiers provided by
                the :class:`trollsift <trollsift.parser.StringFormatter>`
                package may also be used. Any directories in the provided
                pattern will be created if they do not exist. Example::

                    {platform_name}_{sensor}_{name}_{start_time:%Y%m%d_%H%M%S}.tif

            base_dir (str):
                Base destination directories for all created files.
            kwargs (dict): Additional keyword arguments to pass to the
                :class:`~satpy.plugin_base.Plugin` class.

        """
        # Load the config
        Plugin.__init__(self, **kwargs)
        self.info = self.config.get("writer", {})

        if "file_pattern" in self.info:
            warnings.warn(
                "Writer YAML config is using 'file_pattern' which "
                "has been deprecated, use 'filename' instead.",
                stacklevel=2
            )
            self.info["filename"] = self.info.pop("file_pattern")

        if "file_pattern" in kwargs:
            warnings.warn(
                "'file_pattern' has been deprecated, use 'filename' instead.",
                DeprecationWarning,
                stacklevel=2
            )
            filename = kwargs.pop("file_pattern")

        # Use options from the config file if they weren't passed as arguments
        self.name = self.info.get("name", None) if name is None else name
        self.file_pattern = self.info.get("filename", None) if filename is None else filename

        if self.name is None:
            raise ValueError("Writer 'name' not provided")

        self.filename_parser = self.create_filename_parser(base_dir)
        self.register_data_files()

    @classmethod
    def separate_init_kwargs(cls, kwargs):
        """Help separating arguments between init and save methods.

        Currently the :class:`~satpy.scene.Scene` is passed one set of
        arguments to represent the Writer creation and saving steps. This is
        not preferred for Writer structure, but provides a simpler interface
        to users. This method splits the provided keyword arguments between
        those needed for initialization and those needed for the ``save_dataset``
        and ``save_datasets`` method calls.

        Writer subclasses should try to prefer keyword arguments only for the
        save methods only and leave the init keyword arguments to the base
        classes when possible.

        """
        # FUTURE: Don't pass Scene.save_datasets kwargs to init and here
        init_kwargs = {}
        kwargs = kwargs.copy()
        for kw in ["base_dir", "filename", "file_pattern"]:
            if kw in kwargs:
                init_kwargs[kw] = kwargs.pop(kw)
        return init_kwargs, kwargs

    def create_filename_parser(self, base_dir):
        """Create a :class:`trollsift.parser.Parser` object for later use."""
        # just in case a writer needs more complex file patterns
        # Set a way to create filenames if we were given a pattern
        if base_dir and self.file_pattern:
            file_pattern = os.path.join(base_dir, self.file_pattern)
        else:
            file_pattern = self.file_pattern
        return parser.Parser(file_pattern) if file_pattern else None

    @staticmethod
    def _prepare_metadata_for_filename_formatting(attrs):
        if isinstance(attrs.get("sensor"), set):
            attrs["sensor"] = "-".join(sorted(attrs["sensor"]))

    def get_filename(self, **kwargs):
        """Create a filename where output data will be saved.

        Args:
            kwargs (dict): Attributes and other metadata to use for formatting
                the previously provided `filename`.

        """
        if self.filename_parser is None:
            raise RuntimeError("No filename pattern or specific filename provided")
        self._prepare_metadata_for_filename_formatting(kwargs)
        output_filename = self.filename_parser.compose(kwargs)
        dirname = os.path.dirname(output_filename)
        if dirname and not os.path.isdir(dirname):
            LOG.info("Creating output directory: {}".format(dirname))
            os.makedirs(dirname, exist_ok=True)
        return output_filename

    def save_datasets(self, datasets, compute=True, **kwargs):
        """Save all datasets to one or more files.

        Subclasses can use this method to save all datasets to one single
        file or optimize the writing of individual datasets. By default
        this simply calls `save_dataset` for each dataset provided.

        Args:
            datasets (Iterable): Iterable of `xarray.DataArray` objects to
                                 save using this writer.
            compute (bool): If `True` (default), compute all the saves to
                            disk. If `False` then the return value is either
                            a :doc:`dask:delayed` object or two lists to
                            be passed to a :func:`dask.array.store` call.
                            See return values below for more details.
            **kwargs: Keyword arguments to pass to `save_dataset`. See that
                      documentation for more details.

        Returns:
            Value returned depends on `compute` keyword argument. If
            `compute` is `True` the value is the result of either a
            :func:`dask.array.store` operation or a :doc:`dask:delayed`
            compute, typically this is `None`. If `compute` is `False` then
            the result is either a :doc:`dask:delayed` object that can be
            computed with `delayed.compute()` or a two element tuple of
            sources and targets to be passed to :func:`dask.array.store`. If
            `targets` is provided then it is the caller's responsibility to
            close any objects that have a "close" method.

        """
        results = []
        for ds in datasets:
            results.append(self.save_dataset(ds, compute=False, **kwargs))

        if compute:
            LOG.info("Computing and writing results...")
            return compute_writer_results([results])

        targets, sources, delayeds = split_results([results])
        if delayeds:
            # This writer had only delayed writes
            return delayeds
        else:
            return targets, sources

    def save_dataset(self, dataset, filename=None, fill_value=None,
                     compute=True, units=None, **kwargs):
        """Save the ``dataset`` to a given ``filename``.

        This method must be overloaded by the subclass.

        Args:
            dataset (xarray.DataArray): Dataset to save using this writer.
            filename (str): Optionally specify the filename to save this
                            dataset to. If not provided then `filename`
                            which can be provided to the init method will be
                            used and formatted by dataset attributes.
            fill_value (int or float): Replace invalid values in the dataset
                                       with this fill value if applicable to
                                       this writer.
            compute (bool): If `True` (default), compute and save the dataset.
                            If `False` return either a :doc:`dask:delayed`
                            object or tuple of (source, target). See the
                            return values below for more information.
            units (str or None): If not None, will convert the dataset to
                                    the given unit using pint-xarray before
                                    saving. Default is not to do any
                                    conversion.
            **kwargs: Other keyword arguments for this particular writer.

        Returns:
            Value returned depends on `compute`. If `compute` is `True` then
            the return value is the result of computing a
            :doc:`dask:delayed` object or running :func:`dask.array.store`.
            If `compute` is `False` then the returned value is either a
            :doc:`dask:delayed` object that can be computed using
            `delayed.compute()` or a tuple of (source, target) that should be
            passed to :func:`dask.array.store`. If target is provided the
            caller is responsible for calling `target.close()` if the target
            has this method.

        """
        raise NotImplementedError(
            "Writer '%s' has not implemented dataset saving" % (self.name, ))


class ImageWriter(Writer):
    """Base writer for image file formats."""

    def __init__(self, name=None, filename=None, base_dir=None, enhance=None, **kwargs):
        """Initialize image writer object.

        Args:
            name (str): A name for this writer for log and error messages.
                If this writer is configured in a YAML file its name should
                match the name of the YAML file. Writer names may also appear
                in output file attributes.
            filename (str): Filename to save data to. This filename can and
                should specify certain python string formatting fields to
                differentiate between data written to the files. Any
                attributes provided by the ``.attrs`` of a DataArray object
                may be included. Format and conversion specifiers provided by
                the :class:`trollsift <trollsift.parser.StringFormatter>`
                package may also be used. Any directories in the provided
                pattern will be created if they do not exist. Example::

                    {platform_name}_{sensor}_{name}_{start_time:%Y%m%d_%H%M%S}.tif

            base_dir (str):
                Base destination directories for all created files.
            enhance (bool or Enhancer): Whether to automatically enhance
                data to be more visually useful and to fit inside the file
                format being saved to. By default, this will default to using
                the enhancement configuration files found using the default
                :class:`~satpy.enhancements.enhancer.Enhancer` class. This can be set to
                `False` so that no enhancments are performed. This can also
                be an instance of the :class:`~satpy.enhancements.enhancer.Enhancer` class
                if further custom enhancement is needed.

            kwargs (dict): Additional keyword arguments to pass to the
                :class:`~satpy.writers.Writer` base class.

        .. versionchanged:: 0.10

            Deprecated `enhancement_config_file` and 'enhancer' in favor of
            `enhance`. Pass an instance of the `Enhancer` class to `enhance`
            instead.

        """
        super().__init__(name, filename, base_dir, **kwargs)
        if enhance is False:
            # No enhancement
            self.enhancer = False
        elif enhance is None or enhance is True:
            # default enhancement
            enhancement_config = self.info.get("enhancement_config", None)
            self.enhancer = Enhancer(enhancement_config_file=enhancement_config)
        else:
            # custom enhancer
            self.enhancer = enhance

    @classmethod
    def separate_init_kwargs(cls, kwargs):
        """Separate the init kwargs."""
        # FUTURE: Don't pass Scene.save_datasets kwargs to init and here
        init_kwargs, kwargs = super(ImageWriter, cls).separate_init_kwargs(kwargs)
        for kw in ["enhancement_config", "enhance"]:
            if kw in kwargs:
                init_kwargs[kw] = kwargs.pop(kw)
        return init_kwargs, kwargs

    def save_dataset(self, dataset, filename=None, fill_value=None,
                     overlay=None, decorate=None, compute=True, units=None, **kwargs):
        """Save the ``dataset`` to a given ``filename``.

        This method creates an enhanced image using :func:`get_enhanced_image`.
        The image is then passed to :meth:`save_image`. See both of these
        functions for more details on the arguments passed to this method.

        """
        if units is not None:
            import pint_xarray  # noqa
            dataset = dataset.pint.quantify().pint.to(units).pint.dequantify()
        img = get_enhanced_image(dataset.squeeze(), enhance=self.enhancer, overlay=overlay,
                                 decorate=decorate, fill_value=fill_value)
        return self.save_image(img, filename=filename, compute=compute, fill_value=fill_value, **kwargs)

    def save_image(
            self,
            img: XRImage,
            filename: Optional[str] = None,
            compute: bool = True,
            **kwargs
    ):
        """Save Image object to a given ``filename``.

        Args:
            img (trollimage.xrimage.XRImage): Image object to save to disk.
            filename (str): Optionally specify the filename to save this
                            dataset to. It may include string formatting
                            patterns that will be filled in by dataset
                            attributes.
            compute (bool): If `True` (default), compute and save the dataset.
                            If `False` return either a :doc:`dask:delayed`
                            object or tuple of (source, target). See the
                            return values below for more information.
            **kwargs: Other keyword arguments to pass to this writer.

        Returns:
            Value returned depends on `compute`. If `compute` is `True` then
            the return value is the result of computing a
            :doc:`dask:delayed` object or running :func:`dask.array.store`.
            If `compute` is `False` then the returned value is either a
            :doc:`dask:delayed` object that can be computed using
            `delayed.compute()` or a tuple of (source, target) that should be
            passed to :func:`dask.array.store`. If target is provided the the
            caller is responsible for calling `target.close()` if the target
            has this method.

        """
        raise NotImplementedError("Writer '%s' has not implemented image saving" % (self.name,))
