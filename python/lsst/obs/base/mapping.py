# This file is part of obs_base.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (https://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
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
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import re
from collections import OrderedDict

from lsst.afw.image import DecoratedImage, Exposure, Image, MaskedImage
from lsst.daf.base import PropertySet
from lsst.daf.persistence import ButlerLocation, NoResults
from lsst.utils import doImportType

__all__ = ["Mapping", "ImageMapping", "ExposureMapping", "CalibrationMapping", "DatasetMapping"]


class Mapping(object):

    """Mapping is a base class for all mappings.  Mappings are used by
    the Mapper to map (determine a path to some data given some
    identifiers) and standardize (convert data into some standard
    format or type) data, and to query the associated registry to see
    what data is available.

    Subclasses must specify self.storage or else override self.map().

    Public methods: lookup, have, need, getKeys, map

    Mappings are specified mainly by policy.  A Mapping policy should
    consist of:

    template (string): a Python string providing the filename for that
    particular dataset type based on some data identifiers.  In the
    case of redundancy in the path (e.g., file uniquely specified by
    the exposure number, but filter in the path), the
    redundant/dependent identifiers can be looked up in the registry.

    python (string): the Python type for the retrieved data (e.g.
    lsst.afw.image.ExposureF)

    persistable (string): the Persistable registration for the on-disk data
    (e.g. ImageU)

    storage (string, optional): Storage type for this dataset type (e.g.
    "FitsStorage")

    level (string, optional): the level in the camera hierarchy at which the
    data is stored (Amp, Ccd or skyTile), if relevant

    tables (string, optional): a whitespace-delimited list of tables in the
    registry that can be NATURAL JOIN-ed to look up additional
    information.

    Parameters
    ----------
    datasetType : `str`
        Butler dataset type to be mapped.
    policy : `daf_persistence.Policy`
        Mapping Policy.
    registry : `lsst.obs.base.Registry`
        Registry for metadata lookups.
    rootStorage : Storage subclass instance
        Interface to persisted repository data.
    provided : `list` of `str`
        Keys provided by the mapper.
    """

    def __init__(self, datasetType, policy, registry, rootStorage, provided=None):

        if policy is None:
            raise RuntimeError("No policy provided for mapping")

        self.datasetType = datasetType
        self.registry = registry
        self.rootStorage = rootStorage

        self._template = policy["template"]  # Template path
        # in most cases, the template can not be used if it is empty, and is
        # accessed via a property that will raise if it is used while
        # `not self._template`. In this case we *do* allow it to be empty, for
        # the purpose of fetching the key dict so that the mapping can be
        # constructed, so that it can raise if it's invalid. I know it's a
        # little odd, but it allows this template check to be introduced
        # without a major refactor.
        if self._template:
            self.keyDict = dict(
                [
                    (k, _formatMap(v, k, datasetType))
                    for k, v in re.findall(r"\%\((\w+)\).*?([diouxXeEfFgGcrs])", self.template)
                ]
            )
        else:
            self.keyDict = {}
        if provided is not None:
            for p in provided:
                if p in self.keyDict:
                    del self.keyDict[p]
        self.python = policy["python"]  # Python type
        self.persistable = policy["persistable"]  # Persistable type
        self.storage = policy["storage"]
        if "level" in policy:
            self.level = policy["level"]  # Level in camera hierarchy
        if "tables" in policy:
            self.tables = policy.asArray("tables")
        else:
            self.tables = None
        self.range = None
        self.columns = None
        self.obsTimeName = policy["obsTimeName"] if "obsTimeName" in policy else None
        self.recipe = policy["recipe"] if "recipe" in policy else "default"

    @property
    def template(self):
        if self._template:  # template must not be an empty string or None
            return self._template
        else:
            raise RuntimeError(
                f"Template is not defined for the {self.datasetType} dataset type, "
                "it must be set before it can be used."
            )

    def keys(self):
        """Return the dict of keys and value types required by this mapping."""
        return self.keyDict

    def map(self, mapper, dataId, write=False):
        """Standard implementation of map function.

        Parameters
        ----------
        mapper: `lsst.daf.persistence.Mapper`
            Object to be mapped.
        dataId: `dict`
            Dataset identifier.

        Returns
        -------
        lsst.daf.persistence.ButlerLocation
            Location of object that was mapped.
        """
        actualId = self.need(iter(self.keyDict.keys()), dataId)
        usedDataId = {key: actualId[key] for key in self.keyDict.keys()}
        path = mapper._mapActualToPath(self.template, actualId)
        if os.path.isabs(path):
            raise RuntimeError("Mapped path should not be absolute.")
        if not write:
            # This allows mapped files to be compressed, ending in .gz or .fz,
            # without any indication from the policy that the file should be
            # compressed, easily allowing repositories to contain a combination
            # of comporessed and not-compressed files.
            # If needed we can add a policy flag to allow compressed files or
            # not, and perhaps a list of  allowed extensions that may exist
            # at the end of the template.
            for ext in (None, ".gz", ".fz"):
                if ext and path.endswith(ext):
                    continue  # if the path already ends with the extension
                extPath = path + ext if ext else path
                newPath = self.rootStorage.instanceSearch(extPath)
                if newPath:
                    path = newPath
                    break
        assert path, "Fully-qualified filename is empty."

        addFunc = "add_" + self.datasetType  # Name of method for additionalData
        if hasattr(mapper, addFunc):
            addFunc = getattr(mapper, addFunc)
            additionalData = addFunc(self.datasetType, actualId)
            assert isinstance(additionalData, PropertySet), "Bad type for returned data: %s" % (
                type(additionalData),
            )
        else:
            additionalData = None

        return ButlerLocation(
            pythonType=self.python,
            cppType=self.persistable,
            storageName=self.storage,
            locationList=path,
            dataId=actualId.copy(),
            mapper=mapper,
            storage=self.rootStorage,
            usedDataId=usedDataId,
            datasetType=self.datasetType,
            additionalData=additionalData,
        )

    def lookup(self, properties, dataId):
        """Look up properties for in a metadata registry given a partial
        dataset identifier.

        Parameters
        ----------
        properties : `list` of `str`
            What to look up.
        dataId : `dict`
            Dataset identifier

        Returns
        -------
        `list` of `tuple`
            Values of properties.
        """
        if self.registry is None:
            raise RuntimeError("No registry for lookup")

        skyMapKeys = ("tract", "patch")

        where = []
        values = []

        # Prepare to remove skymap entries from properties list.  These must
        # be in the data ID, so we store which ones we're removing and create
        # an OrderedDict that tells us where to re-insert them.  That maps the
        # name of the property to either its index in the properties list
        # *after* the skymap ones have been removed (for entries that aren't
        # skymap ones) or the value from the data ID (for those that are).
        removed = set()
        substitutions = OrderedDict()
        index = 0
        properties = list(properties)  # don't modify the original list
        for p in properties:
            if p in skyMapKeys:
                try:
                    substitutions[p] = dataId[p]
                    removed.add(p)
                except KeyError:
                    raise RuntimeError(
                        "Cannot look up skymap key '%s'; it must be explicitly included in the data ID" % p
                    )
            else:
                substitutions[p] = index
                index += 1
        # Can't actually remove while iterating above, so we do it here.
        for p in removed:
            properties.remove(p)

        fastPath = True
        for p in properties:
            if p not in ("filter", "expTime", "taiObs"):
                fastPath = False
                break
        if fastPath and "visit" in dataId and "raw" in self.tables:
            lookupDataId = {"visit": dataId["visit"]}
            result = self.registry.lookup(properties, "raw_visit", lookupDataId, template=self.template)
        else:
            if dataId is not None:
                for k, v in dataId.items():
                    if self.columns and k not in self.columns:
                        continue
                    if k == self.obsTimeName:
                        continue
                    if k in skyMapKeys:
                        continue
                    where.append((k, "?"))
                    values.append(v)
            lookupDataId = {k[0]: v for k, v in zip(where, values)}
            if self.range:
                # format of self.range is
                # ('?', isBetween-lowKey, isBetween-highKey)
                # here we transform that to {(lowKey, highKey): value}
                lookupDataId[(self.range[1], self.range[2])] = dataId[self.obsTimeName]
            result = self.registry.lookup(properties, self.tables, lookupDataId, template=self.template)
        if not removed:
            return result
        # Iterate over the query results, re-inserting the skymap entries.
        result = [tuple(v if k in removed else item[v] for k, v in substitutions.items()) for item in result]
        return result

    def have(self, properties, dataId):
        """Returns whether the provided data identifier has all
        the properties in the provided list.

        Parameters
        ----------
        properties : `list of `str`
            Properties required.
        dataId : `dict`
            Dataset identifier.

        Returns
        -------
        bool
            True if all properties are present.
        """
        for prop in properties:
            if prop not in dataId:
                return False
        return True

    def need(self, properties, dataId):
        """Ensures all properties in the provided list are present in
        the data identifier, looking them up as needed.  This is only
        possible for the case where the data identifies a single
        exposure.

        Parameters
        ----------
        properties : `list` of `str`
            Properties required.
        dataId : `dict`
            Partial dataset identifier

        Returns
        -------
        `dict`
            Copy of dataset identifier with enhanced values.
        """
        newId = dataId.copy()
        newProps = []  # Properties we don't already have
        for prop in properties:
            if prop not in newId:
                newProps.append(prop)
        if len(newProps) == 0:
            return newId

        lookups = self.lookup(newProps, newId)
        if len(lookups) != 1:
            raise NoResults(
                "No unique lookup for %s from %s: %d matches" % (newProps, newId, len(lookups)),
                self.datasetType,
                dataId,
            )
        for i, prop in enumerate(newProps):
            newId[prop] = lookups[0][i]
        return newId


def _formatMap(ch, k, datasetType):
    """Convert a format character into a Python type."""
    if ch in "diouxX":
        return int
    elif ch in "eEfFgG":
        return float
    elif ch in "crs":
        return str
    else:
        raise RuntimeError(
            "Unexpected format specifier %s for field %s in template for dataset %s" % (ch, k, datasetType)
        )


class ImageMapping(Mapping):
    """ImageMapping is a Mapping subclass for non-camera images.

    Parameters
    ----------
    datasetType : `str`
        Butler dataset type to be mapped.
    policy : `daf_persistence.Policy`
        Mapping Policy.
    registry : `lsst.obs.base.Registry`
        Registry for metadata lookups
    root : `str`
        Path of root directory
    """

    def __init__(self, datasetType, policy, registry, root, **kwargs):
        Mapping.__init__(self, datasetType, policy, registry, root, **kwargs)
        self.columns = policy.asArray("columns") if "columns" in policy else None


class ExposureMapping(Mapping):
    """ExposureMapping is a Mapping subclass for normal exposures.

    Parameters
    ----------
    datasetType : `str`
        Butler dataset type to be mapped.
    policy : `daf_persistence.Policy`
        Mapping Policy.
    registry : `lsst.obs.base.Registry`
        Registry for metadata lookups
    root : `str`
        Path of root directory
    """

    def __init__(self, datasetType, policy, registry, root, **kwargs):
        Mapping.__init__(self, datasetType, policy, registry, root, **kwargs)
        self.columns = policy.asArray("columns") if "columns" in policy else None

    def standardize(self, mapper, item, dataId):
        return mapper._standardizeExposure(self, item, dataId)


class CalibrationMapping(Mapping):
    """CalibrationMapping is a Mapping subclass for calibration-type products.

    The difference is that data properties in the query or template
    can be looked up using a reference Mapping in addition to this one.

    CalibrationMapping Policies can contain the following:

    reference (string, optional)
        a list of tables for finding missing dataset
        identifier components (including the observation time, if a validity
        range is required) in the exposure registry; note that the "tables"
        entry refers to the calibration registry

    refCols (string, optional)
        a list of dataset properties required from the
        reference tables for lookups in the calibration registry

    validRange (bool)
        true if the calibration dataset has a validity range
        specified by a column in the tables of the reference dataset in the
        exposure registry) and two columns in the tables of this calibration
        dataset in the calibration registry)

    obsTimeName (string, optional)
        the name of the column in the reference
        dataset tables containing the observation time (default "taiObs")

    validStartName (string, optional)
        the name of the column in the
        calibration dataset tables containing the start of the validity range
        (default "validStart")

    validEndName (string, optional)
        the name of the column in the
        calibration dataset tables containing the end of the validity range
        (default "validEnd")

    Parameters
    ----------
    datasetType : `str`
        Butler dataset type to be mapped.
    policy : `daf_persistence.Policy`
        Mapping Policy.
    registry : `lsst.obs.base.Registry`
        Registry for metadata lookups
    calibRegistry : `lsst.obs.base.Registry`
        Registry for calibration metadata lookups.
    calibRoot : `str`
        Path of calibration root directory.
    dataRoot : `str`
        Path of data root directory; used for outputs only.
    """

    def __init__(self, datasetType, policy, registry, calibRegistry, calibRoot, dataRoot=None, **kwargs):
        Mapping.__init__(self, datasetType, policy, calibRegistry, calibRoot, **kwargs)
        self.reference = policy.asArray("reference") if "reference" in policy else None
        self.refCols = policy.asArray("refCols") if "refCols" in policy else None
        self.refRegistry = registry
        self.dataRoot = dataRoot
        if "validRange" in policy and policy["validRange"]:
            self.range = ("?", policy["validStartName"], policy["validEndName"])
        if "columns" in policy:
            self.columns = policy.asArray("columns")
        if "filter" in policy:
            self.setFilter = policy["filter"]
        self.metadataKeys = None
        if "metadataKey" in policy:
            self.metadataKeys = policy.asArray("metadataKey")

    def map(self, mapper, dataId, write=False):
        location = Mapping.map(self, mapper, dataId, write=write)
        # Want outputs to be in the output directory
        if write and self.dataRoot:
            location.storage = self.dataRoot
        return location

    def lookup(self, properties, dataId):
        """Look up properties for in a metadata registry given a partial
        dataset identifier.

        Parameters
        ----------
        properties : `list` of `str`
            Properties to look up.
        dataId : `dict`
            Dataset identifier.

        Returns
        -------
        `list` of `tuple`
            Values of properties.
        """

        # Either look up taiObs in reference and then all in calibRegistry
        # Or look up all in registry

        newId = dataId.copy()
        if self.reference is not None:
            where = []
            values = []
            for k, v in dataId.items():
                if self.refCols and k not in self.refCols:
                    continue
                where.append(k)
                values.append(v)

            # Columns we need from the regular registry
            if self.columns is not None:
                columns = set(self.columns)
                for k in dataId.keys():
                    columns.discard(k)
            else:
                columns = set(properties)

            if not columns:
                # Nothing to lookup in reference registry; continue with calib
                # registry
                return Mapping.lookup(self, properties, newId)

            lookupDataId = dict(zip(where, values))
            lookups = self.refRegistry.lookup(columns, self.reference, lookupDataId)
            if len(lookups) != 1:
                raise RuntimeError(
                    "No unique lookup for %s from %s: %d matches" % (columns, dataId, len(lookups))
                )
            if columns == set(properties):
                # Have everything we need
                return lookups
            for i, prop in enumerate(columns):
                newId[prop] = lookups[0][i]
        return Mapping.lookup(self, properties, newId)

    def standardize(self, mapper, item, dataId):
        """Default standardization function for calibration datasets.

        If the item is of a type that should be standardized, the base class
        ``standardizeExposure`` method is called, otherwise the item is
        returned unmodified.

        Parameters
        ----------
        mapping : `lsst.obs.base.Mapping`
            Mapping object to pass through.
        item : object
            Will be standardized if of type lsst.afw.image.Exposure,
            lsst.afw.image.DecoratedImage, lsst.afw.image.Image
            or lsst.afw.image.MaskedImage

        dataId : `dict`
            Dataset identifier

        Returns
        -------
        `lsst.afw.image.Exposure` or item
            The standardized object.
        """
        if issubclass(doImportType(self.python), (Exposure, MaskedImage, Image, DecoratedImage)):
            return mapper._standardizeExposure(self, item, dataId, filter=self.setFilter)
        return item


class DatasetMapping(Mapping):
    """DatasetMapping is a Mapping subclass for non-Exposure datasets that can
    be retrieved by the standard daf_persistence mechanism.

    The differences are that the Storage type must be specified and no
    Exposure standardization is performed.

    The "storage" entry in the Policy is mandatory; the "tables" entry is
    optional; no "level" entry is allowed.

    Parameters
    ----------
    datasetType : `str`
        Butler dataset type to be mapped.
    policy : `daf_persistence.Policy`
        Mapping Policy.
    registry : `lsst.obs.base.Registry`
        Registry for metadata lookups
    root : `str`
        Path of root directory
    """

    def __init__(self, datasetType, policy, registry, root, **kwargs):
        Mapping.__init__(self, datasetType, policy, registry, root, **kwargs)
        self.storage = policy["storage"]  # Storage type
