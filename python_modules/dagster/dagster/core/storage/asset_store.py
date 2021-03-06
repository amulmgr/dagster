import os
import pickle
from abc import abstractmethod
from collections import namedtuple

from dagster import check
from dagster.config import Field
from dagster.config.source import StringSource
from dagster.core.definitions.events import AssetKey, EventMetadataEntry
from dagster.core.definitions.resource import resource
from dagster.core.definitions.solid import SolidDefinition
from dagster.core.events import AssetMaterialization
from dagster.core.storage.object_manager import ObjectManager
from dagster.serdes import whitelist_for_serdes
from dagster.utils import PICKLE_PROTOCOL, mkdir_p
from dagster.utils.backcompat import experimental


@whitelist_for_serdes
class AssetStoreHandle(namedtuple("_AssetStoreHandle", "asset_store_key asset_metadata")):
    def __new__(cls, asset_store_key, asset_metadata=None):
        return super(AssetStoreHandle, cls).__new__(
            cls,
            asset_store_key=check.str_param(asset_store_key, "asset_store_key"),
            asset_metadata=check.opt_dict_param(asset_metadata, "asset_metadata", key_type=str),
        )


class AssetStore(ObjectManager):
    """
    Base class for user-provided asset store.

    Extend this class to handle asset operations. Users should implement ``materialize`` to store a
    data object that can be tracked by the Dagster machinery and ``load`` to retrieve a data
    object.
    """

    def handle_output(self, context, obj):
        return self.set_asset(AssetStoreContext.from_output_context(context), obj)

    def load_input(self, context):
        return self.get_asset(AssetStoreContext.from_load_context(context))

    @abstractmethod
    def set_asset(self, context, obj):
        """The user-defined write method that stores a data object.

        Args:
            context (AssetStoreContext): The context of the step output that produces this asset.
            obj (Any): The data object to be stored.
        """

    @abstractmethod
    def get_asset(self, context):
        """The user-defined read method that loads data given its metadata.
        Args:
            context (AssetStoreContext): The context of the step output that produces this asset.
        """


class VersionedAssetStore(AssetStore):
    """
    Base class for asset store enabled to work with memoized execution. Users should implement the
    ``set_asset`` and ``get_asset`` methods described in the ``AssetStore`` API, and the
    ``has_asset`` method, which returns a boolean representing whether a data object with a version
    corresponding to the context can be found.
    """

    @abstractmethod
    def has_asset(self, context):
        """The user-defined method that returns whether data exists given the metadata.

        Args:
            context (AssetStoreContext): The context of the step performing this check.

        Returns:
            bool: True if there is data present that matches the provided context. False otherwise.
        """


class InMemoryAssetStore(AssetStore):
    def __init__(self):
        self.values = {}

    def set_asset(self, context, obj):
        keys = tuple(context.get_run_scoped_output_identifier())
        self.values[keys] = obj

    def get_asset(self, context):
        keys = tuple(context.get_run_scoped_output_identifier())
        return self.values[keys]


@resource
def mem_asset_store(_):
    return InMemoryAssetStore()


class PickledObjectFilesystemAssetStore(AssetStore):
    """Built-in filesystem asset store that stores and retrieves values using pickling.

    Args:
        base_dir (Optional[str]): base directory where all the step outputs which use this asset
            store will be stored in.
    """

    def __init__(self, base_dir=None):
        self.base_dir = check.opt_str_param(base_dir, "base_dir")
        self.write_mode = "wb"
        self.read_mode = "rb"

    def _get_path(self, context):
        """Automatically construct filepath."""
        keys = context.get_run_scoped_output_identifier()

        return os.path.join(self.base_dir, *keys)

    def set_asset(self, context, obj):
        """Pickle the data and store the object to a file.

        This method omits the AssetMaterialization event so assets generated by it won't be tracked
        by the Asset Catalog.
        """
        check.inst_param(context, "context", AssetStoreContext)

        filepath = self._get_path(context)

        # Ensure path exists
        mkdir_p(os.path.dirname(filepath))

        with open(filepath, self.write_mode) as write_obj:
            pickle.dump(obj, write_obj, PICKLE_PROTOCOL)

    def get_asset(self, context):
        """Unpickle the file and Load it to a data object."""
        check.inst_param(context, "context", AssetStoreContext)

        filepath = self._get_path(context)

        with open(filepath, self.read_mode) as read_obj:
            return pickle.load(read_obj)


@resource(config_schema={"base_dir": Field(StringSource, default_value=".", is_required=False)})
@experimental
def fs_asset_store(init_context):
    """Built-in filesystem asset store that stores and retrieves values using pickling.

    It allows users to specify a base directory where all the step output will be stored in. It
    serializes and deserializes output values (assets) using pickling and automatically constructs
    the filepaths for the assets.

    Example usage:

    1. Specify a pipeline-level asset store using the reserved resource key ``"object_manager"``,
    which will set the given asset store on all solids across a pipeline.

    .. code-block:: python

        @solid
        def solid_a(context, df):
            return df

        @solid
        def solid_b(context, df):
            return df[:5]

        @pipeline(mode_defs=[ModeDefinition(resource_defs={"object_manager": fs_asset_store})])
        def pipe():
            solid_b(solid_a())


    2. Specify asset store on :py:class:`OutputDefinition`, which allows the user to set different
    asset stores on different step outputs.

    .. code-block:: python

        @solid(output_defs=[OutputDefinition(asset_store_key="my_asset_store")])
        def solid_a(context, df):
            return df

        @solid
        def solid_b(context, df):
            return df[:5]

        @pipeline(
            mode_defs=[ModeDefinition(resource_defs={"my_asset_store": fs_asset_store})]
        )
        def pipe():
            solid_b(solid_a())

    """

    return PickledObjectFilesystemAssetStore(init_context.resource_config["base_dir"])


class CustomPathPickledObjectFilesystemAssetStore(AssetStore):
    """Built-in filesystem asset store that stores and retrieves values using pickling and
    allow users to specify file path for outputs.

    Args:
        base_dir (Optional[str]): base directory where all the step outputs which use this asset
            store will be stored in.
    """

    def __init__(self, base_dir=None):
        self.base_dir = check.opt_str_param(base_dir, "base_dir")
        self.write_mode = "wb"
        self.read_mode = "rb"

    def _get_path(self, path):
        return os.path.join(self.base_dir, path)

    def set_asset(self, context, obj):
        """Pickle the data and store the object to a custom file path.

        This method emits an AssetMaterialization event so the assets will be tracked by the
        Asset Catalog.
        """
        check.inst_param(context, "context", AssetStoreContext)
        asset_metadata = context.asset_metadata
        path = check.str_param(asset_metadata.get("path"), "asset_metadata.path")

        filepath = self._get_path(path)

        # Ensure path exists
        mkdir_p(os.path.dirname(filepath))

        with open(filepath, self.write_mode) as write_obj:
            pickle.dump(obj, write_obj, PICKLE_PROTOCOL)

        return AssetMaterialization(
            asset_key=AssetKey([context.pipeline_name, context.step_key, context.output_name]),
            metadata_entries=[EventMetadataEntry.fspath(os.path.abspath(filepath))],
        )

    def get_asset(self, context):
        """Unpickle the file from a given file path and Load it to a data object."""
        check.inst_param(context, "context", AssetStoreContext)
        asset_metadata = context.asset_metadata
        path = check.str_param(asset_metadata.get("path"), "asset_metadata.path")
        filepath = self._get_path(path)

        with open(filepath, self.read_mode) as read_obj:
            return pickle.load(read_obj)


@resource(config_schema={"base_dir": Field(StringSource, default_value=".", is_required=False)})
@experimental
def custom_path_fs_asset_store(init_context):
    """Built-in asset store that allows users to custom output file path per output definition.

    It also allows users to specify a base directory where all the step output will be stored in. It
    serializes and deserializes output values (assets) using pickling and stores the pickled object
    in the user-provided file paths.

    Example usage:

    .. code-block:: python

        @solid(
            output_defs=[
                OutputDefinition(
                    asset_store_key="object_manager", asset_metadata={"path": "path/to/sample_output"}
                )
            ]
        )
        def sample_data(context, df):
            return df[:5]

        @pipeline(
            mode_defs=[
                ModeDefinition(resource_defs={"object_manager": custom_path_fs_asset_store}),
            ],
        )
        def pipe():
            sample_data()
    """
    return CustomPathPickledObjectFilesystemAssetStore(init_context.resource_config["base_dir"])


class VersionedPickledObjectFilesystemAssetStore(VersionedAssetStore):
    def __init__(self, base_dir=None):
        self.base_dir = check.opt_str_param(base_dir, "base_dir")
        self.write_mode = "wb"
        self.read_mode = "rb"

    def _get_path(self, context):
        # automatically construct filepath
        step_key = check.str_param(context.step_key, "context.step_key")
        output_name = check.str_param(context.output_name, "context.output_name")
        version = check.str_param(context.version, "context.version")

        return os.path.join(self.base_dir, step_key, output_name, version)

    def set_asset(self, context, obj):
        """Pickle the data with the associated version, and store the object to a file.

        This method omits the AssetMaterialization event so assets generated by it won't be tracked
        by the Asset Catalog.
        """

        filepath = self._get_path(context)

        # Ensure path exists
        mkdir_p(os.path.dirname(filepath))

        with open(filepath, self.write_mode) as write_obj:
            pickle.dump(obj, write_obj, PICKLE_PROTOCOL)

    def get_asset(self, context):
        """Unpickle the file and Load it to a data object."""

        filepath = self._get_path(context)

        with open(filepath, self.read_mode) as read_obj:
            return pickle.load(read_obj)

    def has_asset(self, context):
        """Returns true if data object exists with the associated version, False otherwise."""

        filepath = self._get_path(context)

        return os.path.exists(filepath) and not os.path.isdir(filepath)


@resource(config_schema={"base_dir": Field(StringSource, default_value=".", is_required=False)})
def versioned_filesystem_asset_store(init_context):
    """Filesystem asset store that utilizes versioning of stored assets.

    It allows users to specify a base directory where all the step output will be stored in. It
    serializes and deserializes output values (assets) using pickling and automatically constructs
    the filepaths for the assets using the provided directory, and the version for a provided step
    output.
    """
    return VersionedPickledObjectFilesystemAssetStore(init_context.resource_config["base_dir"])


class AssetStoreContext(
    namedtuple(
        "_AssetStoreContext",
        "step_key output_name asset_metadata pipeline_name solid_def source_run_id version",
    )
):
    """
    The ``context`` object available to the methods of :py:class:`AssetStore`.

    Attributes:
        step_key (str): The step_key for the compute step.
        output_name (str): The name of the output. (default: 'result').
        asset_metadata ([Dict[str, Any]]): A dict of the metadata that is used for the asset store
            to store or retrieve the data object.
        pipeline_name (str): The name of the pipeline.
        solid_def (SolidDefinition): The definition of the solid that uses the asset store.
        source_run_id (Optional[str]): The id of the run which generates the output.
        version (Optional[str]): The version corresponding to the provided step output.
    """

    def __new__(
        cls,
        step_key,
        output_name,
        asset_metadata,
        pipeline_name,
        solid_def,
        source_run_id=None,
        version=None,
    ):

        return super(AssetStoreContext, cls).__new__(
            cls,
            step_key=check.str_param(step_key, "step_key"),
            output_name=check.str_param(output_name, "output_name"),
            asset_metadata=check.opt_dict_param(asset_metadata, "asset_metadata", key_type=str),
            pipeline_name=check.str_param(pipeline_name, "pipeline_name"),
            solid_def=check.inst_param(solid_def, "solid_def", SolidDefinition),
            source_run_id=check.opt_str_param(source_run_id, "source_run_id"),
            version=check.opt_str_param(version, "version"),
        )

    def get_run_scoped_output_identifier(self):
        """Utility method to get a collection of identifiers that as a whole represent a unique
        step output.

        The unique identifier collection consists of

        - ``source_run_id``: the id of the run which generates the output.
            Note: This method also handles the re-execution memoization logic. If the step that
            generates the output is skipped in the re-execution, the ``run_id`` will be the id
            of its parent run.
        - ``step_key``: the key for a compute step.
        - ``output_name``: the name of the output. (default: 'result').

        Returns:
            List[str, ...]: A list of identifiers, i.e. run id, step key, and output name
        """
        return [self.source_run_id, self.step_key, self.output_name]

    @staticmethod
    def from_output_context(output_context):
        return AssetStoreContext(
            step_key=output_context.step_key,
            output_name=output_context.name,
            asset_metadata=output_context.metadata,
            pipeline_name=output_context.pipeline_name,
            solid_def=output_context.solid_def,
            source_run_id=output_context.run_id,
            version=output_context.version,
        )

    @staticmethod
    def from_load_context(load_context):
        output_context = load_context.upstream_output
        return AssetStoreContext(
            step_key=output_context.step_key,
            output_name=output_context.name,
            asset_metadata=output_context.metadata,
            pipeline_name=output_context.pipeline_name,
            solid_def=load_context.solid_def,
            source_run_id=output_context.run_id,
            version=output_context.version,
        )
