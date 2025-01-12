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

import copy
import os
import re
import traceback
import warnings
import weakref

import lsst.afw.cameraGeom as afwCameraGeom
import lsst.afw.geom as afwGeom
import lsst.afw.image as afwImage
import lsst.daf.base as dafBase
import lsst.daf.persistence as dafPersist
import lsst.log as lsstLog
import lsst.pex.exceptions as pexExcept
from astro_metadata_translator import fix_header
from deprecated.sphinx import deprecated
from lsst.afw.fits import readMetadata
from lsst.afw.table import Schema
from lsst.utils import doImportType, getPackageDir

from ._instrument import Instrument
from .exposureIdInfo import ExposureIdInfo
from .makeRawVisitInfo import MakeRawVisitInfo
from .mapping import CalibrationMapping, DatasetMapping, ExposureMapping, ImageMapping
from .utils import InitialSkyWcsError, createInitialSkyWcs

__all__ = ["CameraMapper", "exposureFromImage"]


class CameraMapper(dafPersist.Mapper):

    """CameraMapper is a base class for mappers that handle images from a
    camera and products derived from them.  This provides an abstraction layer
    between the data on disk and the code.

    Public methods: keys, queryMetadata, getDatasetTypes, map,
    canStandardize, standardize

    Mappers for specific data sources (e.g., CFHT Megacam, LSST
    simulations, etc.) should inherit this class.

    The CameraMapper manages datasets within a "root" directory. Note that
    writing to a dataset present in the input root will hide the existing
    dataset but not overwrite it.  See #2160 for design discussion.

    A camera is assumed to consist of one or more rafts, each composed of
    multiple CCDs.  Each CCD is in turn composed of one or more amplifiers
    (amps).  A camera is also assumed to have a camera geometry description
    (CameraGeom object) as a policy file, a filter description (Filter class
    static configuration) as another policy file.

    Information from the camera geometry and defects are inserted into all
    Exposure objects returned.

    The mapper uses one or two registries to retrieve metadata about the
    images.  The first is a registry of all raw exposures.  This must contain
    the time of the observation.  One or more tables (or the equivalent)
    within the registry are used to look up data identifier components that
    are not specified by the user (e.g. filter) and to return results for
    metadata queries.  The second is an optional registry of all calibration
    data.  This should contain validity start and end entries for each
    calibration dataset in the same timescale as the observation time.

    Subclasses will typically set MakeRawVisitInfoClass and optionally the
    metadata translator class:

    MakeRawVisitInfoClass: a class variable that points to a subclass of
    MakeRawVisitInfo, a functor that creates an
    lsst.afw.image.VisitInfo from the FITS metadata of a raw image.

    translatorClass: The `~astro_metadata_translator.MetadataTranslator`
    class to use for fixing metadata values.  If it is not set an attempt
    will be made to infer the class from ``MakeRawVisitInfoClass``, failing
    that the metadata fixup will try to infer the translator class from the
    header itself.

    Subclasses must provide the following methods:

    _extractDetectorName(self, dataId): returns the detector name for a CCD
    (e.g., "CFHT 21", "R:1,2 S:3,4") as used in the AFW CameraGeom class given
    a dataset identifier referring to that CCD or a subcomponent of it.

    _computeCcdExposureId(self, dataId): see below

    _computeCoaddExposureId(self, dataId, singleFilter): see below

    Subclasses may also need to override the following methods:

    _transformId(self, dataId): transformation of a data identifier
    from colloquial usage (e.g., "ccdname") to proper/actual usage
    (e.g., "ccd"), including making suitable for path expansion (e.g. removing
    commas). The default implementation does nothing.  Note that this
    method should not modify its input parameter.

    getShortCcdName(self, ccdName): a static method that returns a shortened
    name suitable for use as a filename. The default version converts spaces
    to underscores.

    _mapActualToPath(self, template, actualId): convert a template path to an
    actual path, using the actual dataset identifier.

    The mapper's behaviors are largely specified by the policy file.
    See the MapperDictionary.paf for descriptions of the available items.

    The 'exposures', 'calibrations', and 'datasets' subpolicies configure
    mappings (see Mappings class).

    Common default mappings for all subclasses can be specified in the
    "policy/{images,exposures,calibrations,datasets}.yaml" files. This
    provides a simple way to add a product to all camera mappers.

    Functions to map (provide a path to the data given a dataset
    identifier dictionary) and standardize (convert data into some standard
    format or type) may be provided in the subclass as "map_{dataset type}"
    and "std_{dataset type}", respectively.

    If non-Exposure datasets cannot be retrieved using standard
    daf_persistence methods alone, a "bypass_{dataset type}" function may be
    provided in the subclass to return the dataset instead of using the
    "datasets" subpolicy.

    Implementations of map_camera and bypass_camera that should typically be
    sufficient are provided in this base class.

    Notes
    -----
    .. todo::

       Instead of auto-loading the camera at construction time, load it from
       the calibration registry

    Parameters
    ----------
    policy : daf_persistence.Policy,
        Policy with per-camera defaults already merged.
    repositoryDir : string
        Policy repository for the subclassing module (obtained with
        getRepositoryPath() on the per-camera default dictionary).
    root : string, optional
        Path to the root directory for data.
    registry : string, optional
        Path to registry with data's metadata.
    calibRoot : string, optional
        Root directory for calibrations.
    calibRegistry : string, optional
        Path to registry with calibrations' metadata.
    provided : list of string, optional
        Keys provided by the mapper.
    parentRegistry : Registry subclass, optional
        Registry from a parent repository that may be used to look up
        data's metadata.
    repositoryCfg : daf_persistence.RepositoryCfg or None, optional
        The configuration information for the repository this mapper is
        being used with.
    """

    packageName = None

    # a class or subclass of MakeRawVisitInfo, a functor that makes an
    # lsst.afw.image.VisitInfo from the FITS metadata of a raw image
    MakeRawVisitInfoClass = MakeRawVisitInfo

    # a class or subclass of PupilFactory
    PupilFactoryClass = afwCameraGeom.PupilFactory

    # Class to use for metadata translations
    translatorClass = None

    # Gen3 instrument corresponding to this mapper
    # Can be a class or a string with the full name of the class
    _gen3instrument = None

    def __init__(
        self,
        policy,
        repositoryDir,
        root=None,
        registry=None,
        calibRoot=None,
        calibRegistry=None,
        provided=None,
        parentRegistry=None,
        repositoryCfg=None,
    ):

        dafPersist.Mapper.__init__(self)

        self.log = lsstLog.Log.getLogger("lsst.CameraMapper")

        if root:
            self.root = root
        elif repositoryCfg:
            self.root = repositoryCfg.root
        else:
            self.root = None

        repoPolicy = repositoryCfg.policy if repositoryCfg else None
        if repoPolicy is not None:
            policy.update(repoPolicy)

        # Levels
        self.levels = dict()
        if "levels" in policy:
            levelsPolicy = policy["levels"]
            for key in levelsPolicy.names(True):
                self.levels[key] = set(levelsPolicy.asArray(key))
        self.defaultLevel = policy["defaultLevel"]
        self.defaultSubLevels = dict()
        if "defaultSubLevels" in policy:
            self.defaultSubLevels = policy["defaultSubLevels"]

        # Root directories
        if root is None:
            root = "."
        root = dafPersist.LogicalLocation(root).locString()

        self.rootStorage = dafPersist.Storage.makeFromURI(uri=root)

        # If the calibRoot is passed in, use that. If not and it's indicated in
        # the policy, use that. And otherwise, the calibs are in the regular
        # root.
        # If the location indicated by the calib root does not exist, do not
        # create it.
        calibStorage = None
        if calibRoot is not None:
            calibRoot = dafPersist.Storage.absolutePath(root, calibRoot)
            calibStorage = dafPersist.Storage.makeFromURI(uri=calibRoot, create=False)
        else:
            calibRoot = policy.get("calibRoot", None)
            if calibRoot:
                calibStorage = dafPersist.Storage.makeFromURI(uri=calibRoot, create=False)
        if calibStorage is None:
            calibStorage = self.rootStorage

        self.root = root

        # Registries
        self.registry = self._setupRegistry(
            "registry",
            "exposure",
            registry,
            policy,
            "registryPath",
            self.rootStorage,
            searchParents=False,
            posixIfNoSql=(not parentRegistry),
        )
        if not self.registry:
            self.registry = parentRegistry
        needCalibRegistry = policy.get("needCalibRegistry", None)
        if needCalibRegistry:
            if calibStorage:
                self.calibRegistry = self._setupRegistry(
                    "calibRegistry",
                    "calib",
                    calibRegistry,
                    policy,
                    "calibRegistryPath",
                    calibStorage,
                    posixIfNoSql=False,
                )  # NB never use posix for calibs
            else:
                raise RuntimeError(
                    "'needCalibRegistry' is true in Policy, but was unable to locate a repo at "
                    f"calibRoot ivar:{calibRoot} or policy['calibRoot']:{policy.get('calibRoot', None)}"
                )
        else:
            self.calibRegistry = None

        # Dict of valid keys and their value types
        self.keyDict = dict()

        self._initMappings(policy, self.rootStorage, calibStorage, provided=None)
        self._initWriteRecipes()

        # Camera geometry
        self.cameraDataLocation = None  # path to camera geometry config file
        self.camera = self._makeCamera(policy=policy, repositoryDir=repositoryDir)

        # Filter translation table
        self.filters = None

        # verify that the class variable packageName is set before attempting
        # to instantiate an instance
        if self.packageName is None:
            raise ValueError("class variable packageName must not be None")

        self.makeRawVisitInfo = self.MakeRawVisitInfoClass(log=self.log)

        # Assign a metadata translator if one has not been defined by
        # subclass. We can sometimes infer one from the RawVisitInfo
        # class.
        if self.translatorClass is None and hasattr(self.makeRawVisitInfo, "metadataTranslator"):
            self.translatorClass = self.makeRawVisitInfo.metadataTranslator

    def _initMappings(self, policy, rootStorage=None, calibStorage=None, provided=None):
        """Initialize mappings

        For each of the dataset types that we want to be able to read, there
        are methods that can be created to support them:
        * map_<dataset> : determine the path for dataset
        * std_<dataset> : standardize the retrieved dataset
        * bypass_<dataset> : retrieve the dataset (bypassing the usual
          retrieval machinery)
        * query_<dataset> : query the registry

        Besides the dataset types explicitly listed in the policy, we create
        additional, derived datasets for additional conveniences,
        e.g., reading the header of an image, retrieving only the size of a
        catalog.

        Parameters
        ----------
        policy : `lsst.daf.persistence.Policy`
            Policy with per-camera defaults already merged
        rootStorage : `Storage subclass instance`
            Interface to persisted repository data.
        calibRoot : `Storage subclass instance`
            Interface to persisted calib repository data
        provided : `list` of `str`
            Keys provided by the mapper
        """
        # Sub-dictionaries (for exposure/calibration/dataset types)
        imgMappingPolicy = dafPersist.Policy(
            dafPersist.Policy.defaultPolicyFile("obs_base", "ImageMappingDefaults.yaml", "policy")
        )
        expMappingPolicy = dafPersist.Policy(
            dafPersist.Policy.defaultPolicyFile("obs_base", "ExposureMappingDefaults.yaml", "policy")
        )
        calMappingPolicy = dafPersist.Policy(
            dafPersist.Policy.defaultPolicyFile("obs_base", "CalibrationMappingDefaults.yaml", "policy")
        )
        dsMappingPolicy = dafPersist.Policy()

        # Mappings
        mappingList = (
            ("images", imgMappingPolicy, ImageMapping),
            ("exposures", expMappingPolicy, ExposureMapping),
            ("calibrations", calMappingPolicy, CalibrationMapping),
            ("datasets", dsMappingPolicy, DatasetMapping),
        )
        self.mappings = dict()
        for name, defPolicy, cls in mappingList:
            if name in policy:
                datasets = policy[name]

                # Centrally-defined datasets
                defaultsPath = os.path.join(getPackageDir("obs_base"), "policy", name + ".yaml")
                if os.path.exists(defaultsPath):
                    datasets.merge(dafPersist.Policy(defaultsPath))

                mappings = dict()
                setattr(self, name, mappings)
                for datasetType in datasets.names(True):
                    subPolicy = datasets[datasetType]
                    subPolicy.merge(defPolicy)

                    if not hasattr(self, "map_" + datasetType) and "composite" in subPolicy:

                        def compositeClosure(
                            dataId, write=False, mapper=None, mapping=None, subPolicy=subPolicy
                        ):
                            components = subPolicy.get("composite")
                            assembler = subPolicy["assembler"] if "assembler" in subPolicy else None
                            disassembler = subPolicy["disassembler"] if "disassembler" in subPolicy else None
                            python = subPolicy["python"]
                            butlerComposite = dafPersist.ButlerComposite(
                                assembler=assembler,
                                disassembler=disassembler,
                                python=python,
                                dataId=dataId,
                                mapper=self,
                            )
                            for name, component in components.items():
                                butlerComposite.add(
                                    id=name,
                                    datasetType=component.get("datasetType"),
                                    setter=component.get("setter", None),
                                    getter=component.get("getter", None),
                                    subset=component.get("subset", False),
                                    inputOnly=component.get("inputOnly", False),
                                )
                            return butlerComposite

                        setattr(self, "map_" + datasetType, compositeClosure)
                        # for now at least, don't set up any other handling for
                        # this dataset type.
                        continue

                    if name == "calibrations":
                        mapping = cls(
                            datasetType,
                            subPolicy,
                            self.registry,
                            self.calibRegistry,
                            calibStorage,
                            provided=provided,
                            dataRoot=rootStorage,
                        )
                    else:
                        mapping = cls(datasetType, subPolicy, self.registry, rootStorage, provided=provided)

                    if datasetType in self.mappings:
                        raise ValueError(f"Duplicate mapping policy for dataset type {datasetType}")
                    self.keyDict.update(mapping.keys())
                    mappings[datasetType] = mapping
                    self.mappings[datasetType] = mapping
                    if not hasattr(self, "map_" + datasetType):

                        def mapClosure(dataId, write=False, mapper=weakref.proxy(self), mapping=mapping):
                            return mapping.map(mapper, dataId, write)

                        setattr(self, "map_" + datasetType, mapClosure)
                    if not hasattr(self, "query_" + datasetType):

                        def queryClosure(format, dataId, mapping=mapping):
                            return mapping.lookup(format, dataId)

                        setattr(self, "query_" + datasetType, queryClosure)
                    if hasattr(mapping, "standardize") and not hasattr(self, "std_" + datasetType):

                        def stdClosure(item, dataId, mapper=weakref.proxy(self), mapping=mapping):
                            return mapping.standardize(mapper, item, dataId)

                        setattr(self, "std_" + datasetType, stdClosure)

                    def setMethods(suffix, mapImpl=None, bypassImpl=None, queryImpl=None):
                        """Set convenience methods on CameraMapper"""
                        mapName = "map_" + datasetType + "_" + suffix
                        bypassName = "bypass_" + datasetType + "_" + suffix
                        queryName = "query_" + datasetType + "_" + suffix
                        if not hasattr(self, mapName):
                            setattr(self, mapName, mapImpl or getattr(self, "map_" + datasetType))
                        if not hasattr(self, bypassName):
                            if bypassImpl is None and hasattr(self, "bypass_" + datasetType):
                                bypassImpl = getattr(self, "bypass_" + datasetType)
                            if bypassImpl is not None:
                                setattr(self, bypassName, bypassImpl)
                        if not hasattr(self, queryName):
                            setattr(self, queryName, queryImpl or getattr(self, "query_" + datasetType))

                    # Filename of dataset
                    setMethods(
                        "filename",
                        bypassImpl=lambda datasetType, pythonType, location, dataId: [
                            os.path.join(location.getStorage().root, p) for p in location.getLocations()
                        ],
                    )
                    # Metadata from FITS file
                    if subPolicy["storage"] == "FitsStorage":  # a FITS image

                        def getMetadata(datasetType, pythonType, location, dataId):
                            md = readMetadata(location.getLocationsWithRoot()[0])
                            fix_header(md, translator_class=self.translatorClass)
                            return md

                        setMethods("md", bypassImpl=getMetadata)

                        # Add support for configuring FITS compression
                        addName = "add_" + datasetType
                        if not hasattr(self, addName):
                            setattr(self, addName, self.getImageCompressionSettings)

                        if name == "exposures":

                            def getSkyWcs(datasetType, pythonType, location, dataId):
                                fitsReader = afwImage.ExposureFitsReader(location.getLocationsWithRoot()[0])
                                return fitsReader.readWcs()

                            setMethods("wcs", bypassImpl=getSkyWcs)

                            def getRawHeaderWcs(datasetType, pythonType, location, dataId):
                                """Create a SkyWcs from the un-modified raw
                                FITS WCS header keys."""
                                if datasetType[:3] != "raw":
                                    raise dafPersist.NoResults(
                                        "Can only get header WCS for raw exposures.", datasetType, dataId
                                    )
                                return afwGeom.makeSkyWcs(readMetadata(location.getLocationsWithRoot()[0]))

                            setMethods("header_wcs", bypassImpl=getRawHeaderWcs)

                            def getPhotoCalib(datasetType, pythonType, location, dataId):
                                fitsReader = afwImage.ExposureFitsReader(location.getLocationsWithRoot()[0])
                                return fitsReader.readPhotoCalib()

                            setMethods("photoCalib", bypassImpl=getPhotoCalib)

                            def getVisitInfo(datasetType, pythonType, location, dataId):
                                fitsReader = afwImage.ExposureFitsReader(location.getLocationsWithRoot()[0])
                                return fitsReader.readVisitInfo()

                            setMethods("visitInfo", bypassImpl=getVisitInfo)

                            # TODO: remove in DM-27177
                            @deprecated(
                                reason="Replaced with getFilterLabel. Will be removed after v22.",
                                category=FutureWarning,
                                version="v22",
                            )
                            def getFilter(datasetType, pythonType, location, dataId):
                                fitsReader = afwImage.ExposureFitsReader(location.getLocationsWithRoot()[0])
                                return fitsReader.readFilter()

                            setMethods("filter", bypassImpl=getFilter)

                            # TODO: deprecate in DM-27177, remove in DM-27811
                            def getFilterLabel(datasetType, pythonType, location, dataId):
                                fitsReader = afwImage.ExposureFitsReader(location.getLocationsWithRoot()[0])
                                storedFilter = fitsReader.readFilterLabel()

                                # Apply standardization used by full Exposure
                                try:
                                    # mapping is local to enclosing scope
                                    idFilter = mapping.need(["filter"], dataId)["filter"]
                                except dafPersist.NoResults:
                                    idFilter = None
                                bestFilter = self._getBestFilter(storedFilter, idFilter)
                                if bestFilter is not None:
                                    return bestFilter
                                else:
                                    return storedFilter

                            setMethods("filterLabel", bypassImpl=getFilterLabel)

                            setMethods(
                                "detector",
                                mapImpl=lambda dataId, write=False: dafPersist.ButlerLocation(
                                    pythonType="lsst.afw.cameraGeom.CameraConfig",
                                    cppType="Config",
                                    storageName="Internal",
                                    locationList="ignored",
                                    dataId=dataId,
                                    mapper=self,
                                    storage=None,
                                ),
                                bypassImpl=lambda datasetType, pythonType, location, dataId: self.camera[
                                    self._extractDetectorName(dataId)
                                ],
                            )

                            def getBBox(datasetType, pythonType, location, dataId):
                                md = readMetadata(location.getLocationsWithRoot()[0], hdu=1)
                                fix_header(md, translator_class=self.translatorClass)
                                return afwImage.bboxFromMetadata(md)

                            setMethods("bbox", bypassImpl=getBBox)

                        elif name == "images":

                            def getBBox(datasetType, pythonType, location, dataId):
                                md = readMetadata(location.getLocationsWithRoot()[0])
                                fix_header(md, translator_class=self.translatorClass)
                                return afwImage.bboxFromMetadata(md)

                            setMethods("bbox", bypassImpl=getBBox)

                    if subPolicy["storage"] == "FitsCatalogStorage":  # a FITS catalog

                        def getMetadata(datasetType, pythonType, location, dataId):
                            md = readMetadata(
                                os.path.join(location.getStorage().root, location.getLocations()[0]), hdu=1
                            )
                            fix_header(md, translator_class=self.translatorClass)
                            return md

                        setMethods("md", bypassImpl=getMetadata)

                    # Sub-images
                    if subPolicy["storage"] == "FitsStorage":

                        def mapSubClosure(dataId, write=False, mapper=weakref.proxy(self), mapping=mapping):
                            subId = dataId.copy()
                            del subId["bbox"]
                            loc = mapping.map(mapper, subId, write)
                            bbox = dataId["bbox"]
                            llcX = bbox.getMinX()
                            llcY = bbox.getMinY()
                            width = bbox.getWidth()
                            height = bbox.getHeight()
                            loc.additionalData.set("llcX", llcX)
                            loc.additionalData.set("llcY", llcY)
                            loc.additionalData.set("width", width)
                            loc.additionalData.set("height", height)
                            if "imageOrigin" in dataId:
                                loc.additionalData.set("imageOrigin", dataId["imageOrigin"])
                            return loc

                        def querySubClosure(key, format, dataId, mapping=mapping):
                            subId = dataId.copy()
                            del subId["bbox"]
                            return mapping.lookup(format, subId)

                        setMethods("sub", mapImpl=mapSubClosure, queryImpl=querySubClosure)

                    if subPolicy["storage"] == "FitsCatalogStorage":
                        # Length of catalog

                        def getLen(datasetType, pythonType, location, dataId):
                            md = readMetadata(
                                os.path.join(location.getStorage().root, location.getLocations()[0]), hdu=1
                            )
                            fix_header(md, translator_class=self.translatorClass)
                            return md["NAXIS2"]

                        setMethods("len", bypassImpl=getLen)

                        # Schema of catalog
                        if not datasetType.endswith("_schema") and datasetType + "_schema" not in datasets:
                            setMethods(
                                "schema",
                                bypassImpl=lambda datasetType, pythonType, location, dataId: Schema.readFits(
                                    os.path.join(location.getStorage().root, location.getLocations()[0])
                                ),
                            )

    def _computeCcdExposureId(self, dataId):
        """Compute the 64-bit (long) identifier for a CCD exposure.

        Subclasses must override

        Parameters
        ----------
        dataId : `dict`
            Data identifier with visit, ccd.
        """
        raise NotImplementedError()

    def _computeCoaddExposureId(self, dataId, singleFilter):
        """Compute the 64-bit (long) identifier for a coadd.

        Subclasses must override

        Parameters
        ----------
        dataId  : `dict`
            Data identifier with tract and patch.
        singleFilter  : `bool`
            True means the desired ID is for a single-filter coadd, in which
            case dataIdmust contain filter.
        """
        raise NotImplementedError()

    def _search(self, path):
        """Search for path in the associated repository's storage.

        Parameters
        ----------
        path : string
            Path that describes an object in the repository associated with
            this mapper.
            Path may contain an HDU indicator, e.g. 'foo.fits[1]'. The
            indicator will be stripped when searching and so will match
            filenames without the HDU indicator, e.g. 'foo.fits'. The path
            returned WILL contain the indicator though, e.g. ['foo.fits[1]'].

        Returns
        -------
        string
            The path for this object in the repository. Will return None if the
            object can't be found. If the input argument path contained an HDU
            indicator, the returned path will also contain the HDU indicator.
        """
        return self.rootStorage.search(path)

    def backup(self, datasetType, dataId):
        """Rename any existing object with the given type and dataId.

        The CameraMapper implementation saves objects in a sequence of e.g.:

        - foo.fits
        - foo.fits~1
        - foo.fits~2

        All of the backups will be placed in the output repo, however, and will
        not be removed if they are found elsewhere in the _parent chain.  This
        means that the same file will be stored twice if the previous version
        was found in an input repo.
        """

        # Calling PosixStorage directly is not the long term solution in this
        # function, this is work-in-progress on epic DM-6225. The plan is for
        # parentSearch to be changed to 'search', and search only the storage
        # associated with this mapper. All searching of parents will be handled
        # by traversing the container of repositories in Butler.

        def firstElement(list):
            """Get the first element in the list, or None if that can't be
            done.
            """
            return list[0] if list is not None and len(list) else None

        n = 0
        newLocation = self.map(datasetType, dataId, write=True)
        newPath = newLocation.getLocations()[0]
        path = dafPersist.PosixStorage.search(self.root, newPath, searchParents=True)
        path = firstElement(path)
        oldPaths = []
        while path is not None:
            n += 1
            oldPaths.append((n, path))
            path = dafPersist.PosixStorage.search(self.root, "%s~%d" % (newPath, n), searchParents=True)
            path = firstElement(path)
        for n, oldPath in reversed(oldPaths):
            self.rootStorage.copyFile(oldPath, "%s~%d" % (newPath, n))

    def keys(self):
        """Return supported keys.

        Returns
        -------
        iterable
            List of keys usable in a dataset identifier
        """
        return iter(self.keyDict.keys())

    def getKeys(self, datasetType, level):
        """Return a dict of supported keys and their value types for a given
        dataset type at a given level of the key hierarchy.

        Parameters
        ----------
        datasetType :  `str`
            Dataset type or None for all dataset types.
        level :  `str` or None
            Level or None for all levels or '' for the default level for the
            camera.

        Returns
        -------
        `dict`
            Keys are strings usable in a dataset identifier, values are their
            value types.
        """

        # not sure if this is how we want to do this. what if None was
        # intended?
        if level == "":
            level = self.getDefaultLevel()

        if datasetType is None:
            keyDict = copy.copy(self.keyDict)
        else:
            keyDict = self.mappings[datasetType].keys()
        if level is not None and level in self.levels:
            keyDict = copy.copy(keyDict)
            for lev in self.levels[level]:
                if lev in keyDict:
                    del keyDict[lev]
        return keyDict

    def getDefaultLevel(self):
        return self.defaultLevel

    def getDefaultSubLevel(self, level):
        if level in self.defaultSubLevels:
            return self.defaultSubLevels[level]
        return None

    @classmethod
    def getCameraName(cls):
        """Return the name of the camera that this CameraMapper is for."""
        className = str(cls)
        className = className[className.find(".") : -1]
        m = re.search(r"(\w+)Mapper", className)
        if m is None:
            m = re.search(r"class '[\w.]*?(\w+)'", className)
        name = m.group(1)
        return name[:1].lower() + name[1:] if name else ""

    @classmethod
    def getPackageName(cls):
        """Return the name of the package containing this CameraMapper."""
        if cls.packageName is None:
            raise ValueError("class variable packageName must not be None")
        return cls.packageName

    @classmethod
    def getGen3Instrument(cls):
        """Return the gen3 Instrument class equivalent for this gen2 Mapper.

        Returns
        -------
        instr : `type`
            A `~lsst.obs.base.Instrument` class.
        """
        if cls._gen3instrument is None:
            raise NotImplementedError(
                "Please provide a specific implementation for your instrument"
                " to enable conversion of this gen2 repository to gen3"
            )
        if isinstance(cls._gen3instrument, str):
            # Given a string to convert to an instrument class
            cls._gen3instrument = doImportType(cls._gen3instrument)
        if not issubclass(cls._gen3instrument, Instrument):
            raise ValueError(
                f"Mapper {cls} has declared a gen3 instrument class of {cls._gen3instrument}"
                " but that is not an lsst.obs.base.Instrument"
            )
        return cls._gen3instrument

    @classmethod
    def getPackageDir(cls):
        """Return the base directory of this package"""
        return getPackageDir(cls.getPackageName())

    def map_camera(self, dataId, write=False):
        """Map a camera dataset."""
        if self.camera is None:
            raise RuntimeError("No camera dataset available.")
        actualId = self._transformId(dataId)
        return dafPersist.ButlerLocation(
            pythonType="lsst.afw.cameraGeom.CameraConfig",
            cppType="Config",
            storageName="ConfigStorage",
            locationList=self.cameraDataLocation or "ignored",
            dataId=actualId,
            mapper=self,
            storage=self.rootStorage,
        )

    def bypass_camera(self, datasetType, pythonType, butlerLocation, dataId):
        """Return the (preloaded) camera object."""
        if self.camera is None:
            raise RuntimeError("No camera dataset available.")
        return self.camera

    def map_expIdInfo(self, dataId, write=False):
        return dafPersist.ButlerLocation(
            pythonType="lsst.obs.base.ExposureIdInfo",
            cppType=None,
            storageName="Internal",
            locationList="ignored",
            dataId=dataId,
            mapper=self,
            storage=self.rootStorage,
        )

    def bypass_expIdInfo(self, datasetType, pythonType, location, dataId):
        """Hook to retrieve an lsst.obs.base.ExposureIdInfo for an exposure"""
        expId = self.bypass_ccdExposureId(datasetType, pythonType, location, dataId)
        expBits = self.bypass_ccdExposureId_bits(datasetType, pythonType, location, dataId)
        return ExposureIdInfo(expId=expId, expBits=expBits)

    def std_bfKernel(self, item, dataId):
        """Disable standardization for bfKernel

        bfKernel is a calibration product that is numpy array,
        unlike other calibration products that are all images;
        all calibration images are sent through _standardizeExposure
        due to CalibrationMapping, but we don't want that to happen to bfKernel
        """
        return item

    def std_raw(self, item, dataId):
        """Standardize a raw dataset by converting it to an Exposure instead
        of an Image"""
        return self._standardizeExposure(
            self.exposures["raw"], item, dataId, trimmed=False, setVisitInfo=True, setExposureId=True
        )

    def map_skypolicy(self, dataId):
        """Map a sky policy."""
        return dafPersist.ButlerLocation(
            "lsst.pex.policy.Policy", "Policy", "Internal", None, None, self, storage=self.rootStorage
        )

    def std_skypolicy(self, item, dataId):
        """Standardize a sky policy by returning the one we use."""
        return self.skypolicy

    ##########################################################################
    #
    # Utility functions
    #
    ##########################################################################

    def _setupRegistry(
        self, name, description, path, policy, policyKey, storage, searchParents=True, posixIfNoSql=True
    ):
        """Set up a registry (usually SQLite3), trying a number of possible
        paths.

        Parameters
        ----------
        name : string
            Name of registry.
        description: `str`
            Description of registry (for log messages)
        path : string
            Path for registry.
        policy : string
            Policy that contains the registry name, used if path is None.
        policyKey : string
            Key in policy for registry path.
        storage : Storage subclass
            Repository Storage to look in.
        searchParents : bool, optional
            True if the search for a registry should follow any Butler v1
            _parent symlinks.
        posixIfNoSql : bool, optional
            If an sqlite registry is not found, will create a posix registry if
            this is True.

        Returns
        -------
        lsst.daf.persistence.Registry
            Registry object
        """
        if path is None and policyKey in policy:
            path = dafPersist.LogicalLocation(policy[policyKey]).locString()
            if os.path.isabs(path):
                raise RuntimeError("Policy should not indicate an absolute path for registry.")
            if not storage.exists(path):
                newPath = storage.instanceSearch(path)

                newPath = newPath[0] if newPath is not None and len(newPath) else None
                if newPath is None:
                    self.log.warning(
                        "Unable to locate registry at policy path (also looked in root): %s", path
                    )
                path = newPath
            else:
                self.log.warning("Unable to locate registry at policy path: %s", path)
                path = None

        # Old Butler API was to indicate the registry WITH the repo folder,
        # New Butler expects the registry to be in the repo folder. To support
        # Old API, check to see if path starts with root, and if so, strip
        # root from path. Currently only works with PosixStorage
        try:
            root = storage.root
            if path and (path.startswith(root)):
                path = path[len(root + "/") :]
        except AttributeError:
            pass

        # determine if there is an sqlite registry and if not, try the posix
        # registry.
        registry = None

        def search(filename, description):
            """Search for file in storage

            Parameters
            ----------
            filename : `str`
                Filename to search for
            description : `str`
                Description of file, for error message.

            Returns
            -------
            path : `str` or `None`
                Path to file, or None
            """
            result = storage.instanceSearch(filename)
            if result:
                return result[0]
            self.log.debug("Unable to locate %s: %s", description, filename)
            return None

        # Search for a suitable registry database
        if path is None:
            path = search("%s.pgsql" % name, "%s in root" % description)
        if path is None:
            path = search("%s.sqlite3" % name, "%s in root" % description)
        if path is None:
            path = search(os.path.join(".", "%s.sqlite3" % name), "%s in current dir" % description)

        if path is not None:
            if not storage.exists(path):
                newPath = storage.instanceSearch(path)
                newPath = newPath[0] if newPath is not None and len(newPath) else None
                if newPath is not None:
                    path = newPath
            localFileObj = storage.getLocalFile(path)
            self.log.info("Loading %s registry from %s", description, localFileObj.name)
            registry = dafPersist.Registry.create(localFileObj.name)
            localFileObj.close()
        elif not registry and posixIfNoSql:
            try:
                self.log.info("Loading Posix %s registry from %s", description, storage.root)
                registry = dafPersist.PosixRegistry(storage.root)
            except Exception:
                registry = None

        return registry

    def _transformId(self, dataId):
        """Generate a standard ID dict from a camera-specific ID dict.

        Canonical keys include:
        - amp: amplifier name
        - ccd: CCD name (in LSST this is a combination of raft and sensor)
        The default implementation returns a copy of its input.

        Parameters
        ----------
        dataId : `dict`
            Dataset identifier; this must not be modified

        Returns
        -------
        `dict`
            Transformed dataset identifier.
        """

        return dataId.copy()

    def _mapActualToPath(self, template, actualId):
        """Convert a template path to an actual path, using the actual data
        identifier.  This implementation is usually sufficient but can be
        overridden by the subclass.

        Parameters
        ----------
        template : `str`
            Template path
        actualId : `dict`
            Dataset identifier

        Returns
        -------
        `str`
            Pathname
        """

        try:
            transformedId = self._transformId(actualId)
            return template % transformedId
        except Exception as e:
            raise RuntimeError("Failed to format %r with data %r: %s" % (template, transformedId, e))

    @staticmethod
    def getShortCcdName(ccdName):
        """Convert a CCD name to a form useful as a filename

        The default implementation converts spaces to underscores.
        """
        return ccdName.replace(" ", "_")

    def _extractDetectorName(self, dataId):
        """Extract the detector (CCD) name from the dataset identifier.

        The name in question is the detector name used by lsst.afw.cameraGeom.

        Parameters
        ----------
        dataId : `dict`
            Dataset identifier.

        Returns
        -------
        `str`
            Detector name
        """
        raise NotImplementedError("No _extractDetectorName() function specified")

    def _setAmpDetector(self, item, dataId, trimmed=True):
        """Set the detector object in an Exposure for an amplifier.

        Defects are also added to the Exposure based on the detector object.

        Parameters
        ----------
        item : `lsst.afw.image.Exposure`
            Exposure to set the detector in.
        dataId : `dict`
            Dataset identifier
        trimmed : `bool`
            Should detector be marked as trimmed? (ignored)
        """

        return self._setCcdDetector(item=item, dataId=dataId, trimmed=trimmed)

    def _setCcdDetector(self, item, dataId, trimmed=True):
        """Set the detector object in an Exposure for a CCD.

        Parameters
        ----------
        item : `lsst.afw.image.Exposure`
            Exposure to set the detector in.
        dataId : `dict`
            Dataset identifier
        trimmed : `bool`
            Should detector be marked as trimmed? (ignored)
        """
        if item.getDetector() is not None:
            return

        detectorName = self._extractDetectorName(dataId)
        detector = self.camera[detectorName]
        item.setDetector(detector)

    @staticmethod
    def _resolveFilters(definitions, idFilter, filterLabel):
        """Identify the filter(s) consistent with partial filter information.

        Parameters
        ----------
        definitions : `lsst.obs.base.FilterDefinitionCollection`
            The filter definitions in which to search for filters.
        idFilter : `str` or `None`
            The filter information provided in a data ID.
        filterLabel : `lsst.afw.image.FilterLabel` or `None`
            The filter information provided by an exposure; may be incomplete.

        Returns
        -------
        filters : `set` [`lsst.obs.base.FilterDefinition`]
            The set of filters consistent with ``idFilter``
            and ``filterLabel``.
        """
        # Assume none of the filter constraints actually wrong/contradictory.
        # Then taking the intersection of all constraints will give a unique
        # result if one exists.
        matches = set(definitions)
        if idFilter is not None:
            matches.intersection_update(definitions.findAll(idFilter))
        if filterLabel is not None and filterLabel.hasPhysicalLabel():
            matches.intersection_update(definitions.findAll(filterLabel.physicalLabel))
        if filterLabel is not None and filterLabel.hasBandLabel():
            matches.intersection_update(definitions.findAll(filterLabel.bandLabel))
        return matches

    def _getBestFilter(self, storedLabel, idFilter):
        """Estimate the most complete filter information consistent with the
        file or registry.

        Parameters
        ----------
        storedLabel : `lsst.afw.image.FilterLabel` or `None`
            The filter previously stored in the file.
        idFilter : `str` or `None`
            The filter implied by the data ID, if any.

        Returns
        -------
        bestFitler : `lsst.afw.image.FilterLabel` or `None`
            The complete filter to describe the dataset. May be equal to
            ``storedLabel``. `None` if no recommendation can be generated.
        """
        try:
            # getGen3Instrument returns class; need to construct it.
            filterDefinitions = self.getGen3Instrument()().filterDefinitions
        except NotImplementedError:
            filterDefinitions = None

        if filterDefinitions is not None:
            definitions = self._resolveFilters(filterDefinitions, idFilter, storedLabel)
            self.log.debug(
                "Matching filters for id=%r and label=%r are %s.", idFilter, storedLabel, definitions
            )
            if len(definitions) == 1:
                newLabel = list(definitions)[0].makeFilterLabel()
                return newLabel
            elif definitions:
                # Some instruments have many filters for the same band, of
                # which one is known by band name and the others always by
                # afw name (e.g., i, i2).
                nonAfw = {f for f in definitions if f.afw_name is None}
                if len(nonAfw) == 1:
                    newLabel = list(nonAfw)[0].makeFilterLabel()
                    self.log.debug("Assuming %r is the correct match.", newLabel)
                    return newLabel

                self.log.warning("Multiple matches for filter %r with data ID %r.", storedLabel, idFilter)
                # Can we at least add a band?
                # Never expect multiple definitions with same physical filter.
                bands = {d.band for d in definitions}  # None counts as separate result!
                if len(bands) == 1 and storedLabel is None:
                    band = list(bands)[0]
                    return afwImage.FilterLabel(band=band)
                else:
                    return None
            else:
                # Unknown filter, nothing to be done.
                self.log.warning("Cannot reconcile filter %r with data ID %r.", storedLabel, idFilter)
                return None

        # Not practical to recommend a FilterLabel without filterDefinitions

        return None

    def _setFilter(self, mapping, item, dataId):
        """Set the filter information in an Exposure.

        The Exposure should already have had a filter loaded, but the reader
        (in ``afw``) had to act on incomplete information. This method
        cross-checks the filter against the data ID and the standard list
        of filters.

        Parameters
        ----------
        mapping : `lsst.obs.base.Mapping`
            Where to get the data ID filter from.
        item : `lsst.afw.image.Exposure`
            Exposure to set the filter in.
        dataId : `dict`
            Dataset identifier.
        """
        if not (
            isinstance(item, afwImage.ExposureU)
            or isinstance(item, afwImage.ExposureI)
            or isinstance(item, afwImage.ExposureF)
            or isinstance(item, afwImage.ExposureD)
        ):
            return

        itemFilter = item.getFilterLabel()  # may be None
        try:
            idFilter = mapping.need(["filter"], dataId)["filter"]
        except dafPersist.NoResults:
            idFilter = None

        bestFilter = self._getBestFilter(itemFilter, idFilter)
        if bestFilter is not None:
            if bestFilter != itemFilter:
                item.setFilterLabel(bestFilter)
            # Already using bestFilter, avoid unnecessary edits
        elif itemFilter is None:
            # Old Filter cleanup, without the benefit of FilterDefinition
            if self.filters is not None and idFilter in self.filters:
                idFilter = self.filters[idFilter]
            try:
                # TODO: remove in DM-27177; at that point may not be able
                # to process IDs without FilterDefinition.
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=FutureWarning)
                    item.setFilter(afwImage.Filter(idFilter))
            except pexExcept.NotFoundError:
                self.log.warning("Filter %s not defined.  Set to UNKNOWN.", idFilter)

    def _standardizeExposure(
        self, mapping, item, dataId, filter=True, trimmed=True, setVisitInfo=True, setExposureId=False
    ):
        """Default standardization function for images.

        This sets the Detector from the camera geometry
        and optionally set the Filter. In both cases this saves
        having to persist some data in each exposure (or image).

        Parameters
        ----------
        mapping : `lsst.obs.base.Mapping`
            Where to get the values from.
        item : image-like object
            Can be any of lsst.afw.image.Exposure,
            lsst.afw.image.DecoratedImage, lsst.afw.image.Image
            or lsst.afw.image.MaskedImage

        dataId : `dict`
            Dataset identifier
        filter : `bool`
            Set filter? Ignored if item is already an exposure
        trimmed : `bool`
            Should detector be marked as trimmed?
        setVisitInfo : `bool`
            Should Exposure have its VisitInfo filled out from the metadata?
        setExposureId : `bool`
            Should Exposure have its exposure ID filled out from the data ID?

        Returns
        -------
        `lsst.afw.image.Exposure`
            The standardized Exposure.
        """
        try:
            exposure = exposureFromImage(
                item,
                dataId,
                mapper=self,
                logger=self.log,
                setVisitInfo=setVisitInfo,
                setFilter=filter,
                setExposureId=setExposureId,
            )
        except Exception as e:
            self.log.error("Could not turn item=%r into an exposure: %s", item, e)
            raise

        if mapping.level.lower() == "amp":
            self._setAmpDetector(exposure, dataId, trimmed)
        elif mapping.level.lower() == "ccd":
            self._setCcdDetector(exposure, dataId, trimmed)

        # We can only create a WCS if it doesn't already have one and
        # we have either a VisitInfo or exposure metadata.
        # Do not calculate a WCS if this is an amplifier exposure
        if (
            mapping.level.lower() != "amp"
            and exposure.getWcs() is None
            and (exposure.getInfo().getVisitInfo() is not None or exposure.getMetadata().toDict())
        ):
            self._createInitialSkyWcs(exposure)

        if filter:
            self._setFilter(mapping, exposure, dataId)

        return exposure

    def _createSkyWcsFromMetadata(self, exposure):
        """Create a SkyWcs from the FITS header metadata in an Exposure.

        Parameters
        ----------
        exposure : `lsst.afw.image.Exposure`
            The exposure to get metadata from, and attach the SkyWcs to.
        """
        metadata = exposure.getMetadata()
        fix_header(metadata, translator_class=self.translatorClass)
        try:
            wcs = afwGeom.makeSkyWcs(metadata, strip=True)
            exposure.setWcs(wcs)
        except pexExcept.TypeError as e:
            # See DM-14372 for why this is debug and not warn (e.g. calib
            # files without wcs metadata).
            self.log.debug(
                "wcs set to None; missing information found in metadata to create a valid wcs: %s",
                e.args[0],
            )
        # ensure any WCS values stripped from the metadata are removed in the
        # exposure
        exposure.setMetadata(metadata)

    def _createInitialSkyWcs(self, exposure):
        """Create a SkyWcs from the boresight and camera geometry.

        If the boresight or camera geometry do not support this method of
        WCS creation, this falls back on the header metadata-based version
        (typically a purely linear FITS crval/crpix/cdmatrix WCS).

        Parameters
        ----------
        exposure : `lsst.afw.image.Exposure`
            The exposure to get data from, and attach the SkyWcs to.
        """
        # Always use try to use metadata first, to strip WCS keys from it.
        self._createSkyWcsFromMetadata(exposure)

        if exposure.getInfo().getVisitInfo() is None:
            msg = "No VisitInfo; cannot access boresight information. Defaulting to metadata-based SkyWcs."
            self.log.warning(msg)
            return
        try:
            newSkyWcs = createInitialSkyWcs(exposure.getInfo().getVisitInfo(), exposure.getDetector())
            exposure.setWcs(newSkyWcs)
        except InitialSkyWcsError as e:
            msg = "Cannot create SkyWcs using VisitInfo and Detector, using metadata-based SkyWcs: %s"
            self.log.warning(msg, e)
            self.log.debug("Exception was: %s", traceback.TracebackException.from_exception(e))
            if e.__context__ is not None:
                self.log.debug(
                    "Root-cause Exception was: %s", traceback.TracebackException.from_exception(e.__context__)
                )

    def _makeCamera(self, policy, repositoryDir):
        """Make a camera (instance of lsst.afw.cameraGeom.Camera) describing
        the camera geometry

        Also set self.cameraDataLocation, if relevant (else it can be left
        None).

        This implementation assumes that policy contains an entry "camera"
        that points to the subdirectory in this package of camera data;
        specifically, that subdirectory must contain:
        - a file named `camera.py` that contains persisted camera config
        - ampInfo table FITS files, as required by
          lsst.afw.cameraGeom.makeCameraFromPath

        Parameters
        ----------
        policy : `lsst.daf.persistence.Policy`
             Policy with per-camera defaults already merged
             (PexPolicy only for backward compatibility).
        repositoryDir : `str`
            Policy repository for the subclassing module (obtained with
            getRepositoryPath() on the per-camera default dictionary).
        """
        if "camera" not in policy:
            raise RuntimeError("Cannot find 'camera' in policy; cannot construct a camera")
        cameraDataSubdir = policy["camera"]
        self.cameraDataLocation = os.path.normpath(os.path.join(repositoryDir, cameraDataSubdir, "camera.py"))
        cameraConfig = afwCameraGeom.CameraConfig()
        cameraConfig.load(self.cameraDataLocation)
        ampInfoPath = os.path.dirname(self.cameraDataLocation)
        return afwCameraGeom.makeCameraFromPath(
            cameraConfig=cameraConfig,
            ampInfoPath=ampInfoPath,
            shortNameFunc=self.getShortCcdName,
            pupilFactoryClass=self.PupilFactoryClass,
        )

    def getRegistry(self):
        """Get the registry used by this mapper.

        Returns
        -------
        Registry or None
            The registry used by this mapper for this mapper's repository.
        """
        return self.registry

    def getImageCompressionSettings(self, datasetType, dataId):
        """Stuff image compression settings into a daf.base.PropertySet

        This goes into the ButlerLocation's "additionalData", which gets
        passed into the boost::persistence framework.

        Parameters
        ----------
        datasetType : `str`
            Type of dataset for which to get the image compression settings.
        dataId : `dict`
            Dataset identifier.

        Returns
        -------
        additionalData : `lsst.daf.base.PropertySet`
            Image compression settings.
        """
        mapping = self.mappings[datasetType]
        recipeName = mapping.recipe
        storageType = mapping.storage
        if storageType not in self._writeRecipes:
            return dafBase.PropertySet()
        if recipeName not in self._writeRecipes[storageType]:
            raise RuntimeError(
                "Unrecognized write recipe for datasetType %s (storage type %s): %s"
                % (datasetType, storageType, recipeName)
            )
        recipe = self._writeRecipes[storageType][recipeName].deepCopy()
        seed = hash(tuple(dataId.items())) % 2**31
        for plane in ("image", "mask", "variance"):
            if recipe.exists(plane + ".scaling.seed") and recipe.getScalar(plane + ".scaling.seed") == 0:
                recipe.set(plane + ".scaling.seed", seed)
        return recipe

    def _initWriteRecipes(self):
        """Read the recipes for writing files

        These recipes are currently used for configuring FITS compression,
        but they could have wider uses for configuring different flavors
        of the storage types. A recipe is referred to by a symbolic name,
        which has associated settings. These settings are stored as a
        `PropertySet` so they can easily be passed down to the
        boost::persistence framework as the "additionalData" parameter.

        The list of recipes is written in YAML. A default recipe and
        some other convenient recipes are in obs_base/policy/writeRecipes.yaml
        and these may be overridden or supplemented by the individual obs_*
        packages' own policy/writeRecipes.yaml files.

        Recipes are grouped by the storage type. Currently, only the
        ``FitsStorage`` storage type uses recipes, which uses it to
        configure FITS image compression.

        Each ``FitsStorage`` recipe for FITS compression should define
        "image", "mask" and "variance" entries, each of which may contain
        "compression" and "scaling" entries. Defaults will be provided for
        any missing elements under "compression" and "scaling".

        The allowed entries under "compression" are:

        * algorithm (string): compression algorithm to use
        * rows (int): number of rows per tile (0 = entire dimension)
        * columns (int): number of columns per tile (0 = entire dimension)
        * quantizeLevel (float): cfitsio quantization level

        The allowed entries under "scaling" are:

        * algorithm (string): scaling algorithm to use
        * bitpix (int): bits per pixel (0,8,16,32,64,-32,-64)
        * fuzz (bool): fuzz the values when quantising floating-point values?
        * seed (long): seed for random number generator when fuzzing
        * maskPlanes (list of string): mask planes to ignore when doing
          statistics
        * quantizeLevel: divisor of the standard deviation for STDEV_* scaling
        * quantizePad: number of stdev to allow on the low side (for
          STDEV_POSITIVE/NEGATIVE)
        * bscale: manually specified BSCALE (for MANUAL scaling)
        * bzero: manually specified BSCALE (for MANUAL scaling)

        A very simple example YAML recipe:

            FitsStorage:
              default:
                image: &default
                  compression:
                    algorithm: GZIP_SHUFFLE
                mask: *default
                variance: *default
        """
        recipesFile = os.path.join(getPackageDir("obs_base"), "policy", "writeRecipes.yaml")
        recipes = dafPersist.Policy(recipesFile)
        supplementsFile = os.path.join(self.getPackageDir(), "policy", "writeRecipes.yaml")
        validationMenu = {
            "FitsStorage": validateRecipeFitsStorage,
        }
        if os.path.exists(supplementsFile) and supplementsFile != recipesFile:
            supplements = dafPersist.Policy(supplementsFile)
            # Don't allow overrides, only supplements
            for entry in validationMenu:
                intersection = set(recipes[entry].names()).intersection(set(supplements.names()))
                if intersection:
                    raise RuntimeError(
                        "Recipes provided in %s section %s may not override those in %s: %s"
                        % (supplementsFile, entry, recipesFile, intersection)
                    )
            recipes.update(supplements)

        self._writeRecipes = {}
        for storageType in recipes.names(True):
            if "default" not in recipes[storageType]:
                raise RuntimeError(
                    "No 'default' recipe defined for storage type %s in %s" % (storageType, recipesFile)
                )
            self._writeRecipes[storageType] = validationMenu[storageType](recipes[storageType])


def exposureFromImage(
    image, dataId=None, mapper=None, logger=None, setVisitInfo=True, setFilter=False, setExposureId=False
):
    """Generate an Exposure from an image-like object

    If the image is a DecoratedImage then also set its metadata
    (Image and MaskedImage are missing the necessary metadata
    and Exposure already has those set)

    Parameters
    ----------
    image : Image-like object
        Can be one of lsst.afw.image.DecoratedImage, Image, MaskedImage or
        Exposure.
    dataId : `dict`, optional
        The data ID identifying the visit of the image.
    mapper : `lsst.obs.base.CameraMapper`, optional
        The mapper with which to convert the image.
    logger : `lsst.log.Log`, optional
        An existing logger to which to send output.
    setVisitInfo : `bool`, optional
        If `True`, create and attach a `lsst.afw.image.VisitInfo` to the
        result. Ignored if ``image`` is an `~lsst.afw.image.Exposure` with an
        existing ``VisitInfo``.
    setFilter : `bool`, optional
        If `True`, create and attach a `lsst.afw.image.FilterLabel` to the
        result. Converts non-``FilterLabel`` information provided in ``image``.
        Ignored if ``image`` is an `~lsst.afw.image.Exposure` with existing
        filter information.
    setExposureId : `bool`, optional
        If `True`, create and set an exposure ID from ``dataID``. Ignored if
        ``image`` is an `~lsst.afw.image.Exposure` with an existing ID.

    Returns
    -------
    `lsst.afw.image.Exposure`
        Exposure containing input image.
    """
    translatorClass = None
    if mapper is not None:
        translatorClass = mapper.translatorClass

    metadata = None
    if isinstance(image, afwImage.MaskedImage):
        exposure = afwImage.makeExposure(image)
    elif isinstance(image, afwImage.DecoratedImage):
        exposure = afwImage.makeExposure(afwImage.makeMaskedImage(image.getImage()))
        metadata = image.getMetadata()
        fix_header(metadata, translator_class=translatorClass)
        exposure.setMetadata(metadata)
    elif isinstance(image, afwImage.Exposure):
        exposure = image
        metadata = exposure.getMetadata()
        fix_header(metadata, translator_class=translatorClass)
    else:  # Image
        exposure = afwImage.makeExposure(afwImage.makeMaskedImage(image))

    # set exposure ID if we can
    if setExposureId and not exposure.info.hasId() and mapper is not None:
        try:
            exposureId = mapper._computeCcdExposureId(dataId)
            exposure.info.id = exposureId
        except NotImplementedError:
            logger.warning("Could not set exposure ID; mapper does not support it.")

    if metadata is not None:
        # set filter if we can
        if setFilter and mapper is not None and exposure.getFilterLabel() is None:
            # Translate whatever was in the metadata
            if "FILTER" in metadata:
                oldFilter = metadata["FILTER"]
                idFilter = dataId["filter"] if "filter" in dataId else None
                # oldFilter may not be physical, but _getBestFilter always goes
                # through the FilterDefinitions instead of returning
                # unvalidated input.
                filter = mapper._getBestFilter(afwImage.FilterLabel(physical=oldFilter), idFilter)
                if filter is not None:
                    exposure.setFilterLabel(filter)
        # set VisitInfo if we can
        if setVisitInfo and exposure.getInfo().getVisitInfo() is None:
            if mapper is None:
                if not logger:
                    logger = lsstLog.Log.getLogger("lsst.CameraMapper")
                logger.warn("I can only set the VisitInfo if you provide a mapper")
            else:
                exposureId = mapper._computeCcdExposureId(dataId)
                visitInfo = mapper.makeRawVisitInfo(md=metadata, exposureId=exposureId)

                exposure.getInfo().setVisitInfo(visitInfo)

    return exposure


def validateRecipeFitsStorage(recipes):
    """Validate recipes for FitsStorage

    The recipes are supplemented with default values where appropriate.

    TODO: replace this custom validation code with Cerberus (DM-11846)

    Parameters
    ----------
    recipes : `lsst.daf.persistence.Policy`
        FitsStorage recipes to validate.

    Returns
    -------
    validated : `lsst.daf.base.PropertySet`
        Validated FitsStorage recipe.

    Raises
    ------
    `RuntimeError`
        If validation fails.
    """
    # Schemas define what should be there, and the default values (and by the
    # default value, the expected type).
    compressionSchema = {
        "algorithm": "NONE",
        "rows": 1,
        "columns": 0,
        "quantizeLevel": 0.0,
    }
    scalingSchema = {
        "algorithm": "NONE",
        "bitpix": 0,
        "maskPlanes": ["NO_DATA"],
        "seed": 0,
        "quantizeLevel": 4.0,
        "quantizePad": 5.0,
        "fuzz": True,
        "bscale": 1.0,
        "bzero": 0.0,
    }

    def checkUnrecognized(entry, allowed, description):
        """Check to see if the entry contains unrecognised keywords"""
        unrecognized = set(entry.keys()) - set(allowed)
        if unrecognized:
            raise RuntimeError(
                "Unrecognized entries when parsing image compression recipe %s: %s"
                % (description, unrecognized)
            )

    validated = {}
    for name in recipes.names(True):
        checkUnrecognized(recipes[name], ["image", "mask", "variance"], name)
        rr = dafBase.PropertySet()
        validated[name] = rr
        for plane in ("image", "mask", "variance"):
            checkUnrecognized(recipes[name][plane], ["compression", "scaling"], name + "->" + plane)

            for settings, schema in (("compression", compressionSchema), ("scaling", scalingSchema)):
                prefix = plane + "." + settings
                if settings not in recipes[name][plane]:
                    for key in schema:
                        rr.set(prefix + "." + key, schema[key])
                    continue
                entry = recipes[name][plane][settings]
                checkUnrecognized(entry, schema.keys(), name + "->" + plane + "->" + settings)
                for key in schema:
                    value = type(schema[key])(entry[key]) if key in entry else schema[key]
                    rr.set(prefix + "." + key, value)
    return validated
