import datetime
import logging
import warnings
import json
from functools import partial

from attr import validators, asdict
from frozenlist2 import frozenlist
from more_executors.futures import f_proxy, f_map, f_flat_map

from .repo_lock import RepoLock
from ..attr import pulp_attrib, PULP2_FIELD, PULP2_MUTABLE
from ..common import PulpObject, Deletable, DetachedException
from ..convert import frozenlist_or_none_converter
from ..distributor import Distributor
from ...criteria import Criteria, Matcher
from ...schema import load_schema
from ... import compat_attr as attr
from ...hooks import pm
from ...util import dict_put, lookup, ABSENT


LOG = logging.getLogger("pubtools.pulplib")

REPO_CLASSES = {}


def repo_type(pulp_type):
    # decorator for Repository subclasses, registers against a
    # particular value of notes._repo-type
    def decorate(klass):
        REPO_CLASSES[pulp_type] = klass
        return klass

    return decorate


@attr.s(kw_only=True, frozen=True)
class PublishOptions(object):
    """Options controlling a repository
    :meth:`~pubtools.pulplib.Repository.publish`.
    """

    force = pulp_attrib(default=None, type=bool)
    """If True, Pulp should publish all data within a repository, rather than attempting
    to publish only changed data (or even skipping the publish).

    Setting ``force=True`` may have a major performance impact when publishing large repos.
    """

    clean = pulp_attrib(default=None, type=bool)
    """If True, certain publish tasks will not only publish new/changed content, but
    will also attempt to erase formerly published content which is no longer present
    in the repo.

    Setting ``clean=True`` generally implies ``force=True``.
    """

    origin_only = pulp_attrib(default=None, type=bool)
    """If ``True``, Pulp should only update the content units / origin path on
    remote hosts.

    Only relevant if a repository has one or more distributors where
    :meth:`~pubtools.pulplib.Distributor.is_rsync` is ``True``.
    """

    rsync_extra_args = pulp_attrib(
        default=None, type=list, converter=frozenlist_or_none_converter
    )
    """If present, provide these additional arguments to any rsync commands run
    during publish.

    Ignored when rsync is not used.
    """


@attr.s(kw_only=True, frozen=True)
class SyncOptions(object):
    """Options controlling a repository
    :meth:`~pubtools.pulplib.Repository.sync`.

    .. seealso:: Subclasses for specific repository
                 types: :py:class:`~pubtools.pulplib.FileSyncOptions`,
                 :py:class:`~pubtools.pulplib.YumSyncOptions`,
                 :py:class:`~pubtools.pulplib.ContainerSyncOptions`
    """

    feed = pulp_attrib(type=str)
    """URL where the repository's content will be synchronized from.
    """

    ssl_validation = pulp_attrib(default=None, type=bool)
    """Indicates if the server's SSL certificate is verified against the CA certificate uploaded.
    """

    ssl_ca_cert = pulp_attrib(default=None, type=str)
    """CA certificate string used to validate the feed source's SSL certificate
    """

    ssl_client_cert = pulp_attrib(default=None, type=str)
    """Certificate used as the client certificate when synchronizing the repository
    """

    ssl_client_key = pulp_attrib(default=None, type=str)
    """Private key to the certificate specified in ssl_client_cert
    """

    max_speed = pulp_attrib(default=None, type=int)
    """The maximum download speed in bytes/sec for a task (such as a sync).

    Default is None
    """

    proxy_host = pulp_attrib(default=None, type=str)
    """A string representing the URL of the proxy server that should be used when synchronizing
    """

    proxy_port = pulp_attrib(default=None, type=int)
    """An integer representing the port that should be used when connecting to proxy_host.
    """

    proxy_username = pulp_attrib(default=None, type=str)
    """A string representing the username that should be used to authenticate with the proxy server
    """

    proxy_password = pulp_attrib(default=None, type=str)
    """A string representing the password that should be used to authenticate with the proxy server
    """

    basic_auth_username = pulp_attrib(default=None, type=str)
    """Username to authenticate with source which supports basic authentication.
    """

    basic_auth_password = pulp_attrib(default=None, type=str)
    """Password to authenticate with source which supports basic authentication.
    """


def pv_converter(versions):
    # Converter for values in a product_versions field.
    #
    # We try to sort numerically while decomposing dotted versions into
    # their components, so e.g. "8.10" is sorted later than "8.8".
    # However, we don't know for sure which version strings might be stored
    # in the field (or even non-strings), so we can fall back to a generic
    # string sort.

    # Everything is initially interpreted as a string regardless of how
    # it was stored.
    versions = [str(v) for v in versions]

    try:
        return sorted(
            versions, key=lambda version: [int(c) for c in str(version).split(".")]
        )
    except ValueError:
        return sorted(versions)


@attr.s(kw_only=True, frozen=True)
class Repository(PulpObject, Deletable):
    """Represents a Pulp repository."""

    _SCHEMA = load_schema("repository")

    # The distributors (by ID) which should be activated when publishing this repo.
    # Order matters. Distributors which don't exist will be ignored.
    _PUBLISH_DISTRIBUTORS = [
        "iso_distributor",
        "yum_distributor",
        "cdn_distributor",
        "cdn_distributor_unprotected",
        "docker_web_distributor_name_cli",
    ]

    id = pulp_attrib(type=str, pulp_field="id")
    """ID of this repository (str)."""

    type = pulp_attrib(default=None, type=str, pulp_field="notes._repo-type")
    """Type of this repository (str).

    This is a brief string denoting the content / Pulp plugin type used with
    this repository, e.g. ``rpm-repo``.
    """

    created = pulp_attrib(
        default=None, type=datetime.datetime, pulp_field="notes.created"
    )
    """:class:`~datetime.datetime` in UTC at which this repository was created,
    or None if this information is unavailable.
    """

    distributors = pulp_attrib(
        default=attr.Factory(frozenlist),
        type=list,
        pulp_field="distributors",
        converter=frozenlist,
        pulp_py_converter=lambda ds: frozenlist([Distributor.from_data(d) for d in ds]),
        # It's too noisy to let repr descend into sub-objects
        repr=False,
    )
    """list of :class:`~pubtools.pulplib.Distributor` objects belonging to this
    repository.
    """

    eng_product_id = pulp_attrib(
        default=None,
        type=int,
        pulp_field="notes.eng_product",
        pulp_py_converter=int,
        py_pulp_converter=str,
    )
    """ID of the product to which this repository belongs (if any)."""

    relative_url = pulp_attrib(default=None, type=str)
    """Default publish URL for this repository, relative to the Pulp content root."""

    mutable_urls = pulp_attrib(
        default=attr.Factory(frozenlist), type=list, converter=frozenlist
    )
    """A list of URLs relative to repository publish root which are expected
    to change at every publish (if any content of repo changed)."""

    is_sigstore = pulp_attrib(default=False, type=bool)
    """True if this is a sigstore repository, used for container image manifest
    signatures.

    .. deprecated:: 2.24.0
       The signatures are not stored in a Pulp repository any more.
    """

    is_temporary = pulp_attrib(
        default=False,
        type=bool,
        validator=validators.instance_of(bool),
        pulp_field="notes.pub_temp_repo",
    )
    """True if this is a temporary repository.

    A temporary repository is a repository created by release-engineering tools
    for temporary use during certain workflows.  Such repos are not expected to
    be published externally and generally should have a lifetime of a few days
    or less.

    .. versionadded:: 1.3.0
    """

    signing_keys = pulp_attrib(
        default=attr.Factory(frozenlist),
        type=list,
        pulp_field="notes.signatures",
        pulp_py_converter=lambda sigs: sigs.split(","),
        py_pulp_converter=",".join,
        converter=lambda keys: frozenlist([k.strip() for k in keys]),
    )
    """A list of GPG signing key IDs used to sign content in this repository."""

    skip_rsync_repodata = pulp_attrib(default=False, type=bool)
    """True if this repository is explicitly configured such that a publish of
    this repository will not publish repository metadata to remote hosts.
    """

    content_set = pulp_attrib(default=None, type=str, pulp_field="notes.content_set")
    """Name of content set that is associated with this repository."""

    arch = pulp_attrib(default=None, type=str, pulp_field="notes.arch")
    """The primary architecture of content within this repository (e.g. 'x86_64').

    .. versionadded:: 2.29.0
    """

    platform_full_version = pulp_attrib(
        default=None, type=str, pulp_field="notes.platform_full_version"
    )
    """A version string associated with the repository.

    This field should be used with care, as the semantics are not well defined.
    It is often, but not always, equal to the $releasever yum variable associated
    with a repository.

    Due to the unclear meaning of this field, it's strongly recommended to avoid
    making use of it in any new code.

    .. versionadded:: 2.29.0
    """

    product_versions = pulp_attrib(
        default=None,
        type=list,
        pulp_field="notes.product_versions",
        pulp_py_converter=json.loads,
        py_pulp_converter=partial(json.dumps, separators=(",", ":")),
        converter=partial(frozenlist_or_none_converter, map_fn=pv_converter),
        mutable=True,
    )
    """A list of product versions associated with this repository.

    The versions found in this list are derived from the product versions found in any
    product certificates (productid) historically uploaded to this repository and
    related repositories.

    This field is **mutable** and may be set by :meth:`~Client.update_repository`.

    .. versionadded:: 2.29.0
    """

    include_in_download_service = pulp_attrib(
        default=False,
        type=bool,
        mutable=True,
        pulp_field="notes.include_in_download_service",
        pulp_py_converter=lambda x: x == "True",
        py_pulp_converter=str,
    )
    """Flag indicating whether the repository is visible in production instance
    of download service.

    .. versionadded:: 2.34.0
    """

    include_in_download_service_preview = pulp_attrib(
        default=False,
        type=bool,
        mutable=True,
        pulp_field="notes.include_in_download_service_preview",
        pulp_py_converter=lambda x: x == "True",
        py_pulp_converter=str,
    )
    """Flag indicating whether the repository is visible in staging instance
    of download service.

    .. versionadded:: 2.34.0
    """

    @distributors.validator
    def _check_repo_id(self, _, value):
        # checks if distributor's repository id is same as the repository it
        # is attached to
        for distributor in value:
            if not distributor.repo_id:
                return
            if distributor.repo_id == self.id:
                return
            raise ValueError(
                "repo_id doesn't match for %s. repo_id: %s, distributor.repo_id: %s"
                % (distributor.id, self.id, distributor.repo_id)
            )

    @property
    def _distributors_by_id(self):
        out = {}
        for dist in self.distributors:
            out[dist.id] = dist
        return out

    @classmethod
    def _mutable_note_fields(cls):
        # Returns the subset of fields on this class which are stored under the
        # notes dict and considered mutable, and thus can potentially be updated.
        return [
            fld
            for fld in attr.fields(cls)
            if fld.metadata.get(PULP2_FIELD, "").startswith("notes.")
            and fld.metadata.get(PULP2_MUTABLE)
        ]

    @property
    def _mutable_notes(self):
        # Returns notes dict containing only mutable notes, appropriate
        # for updating the repo.

        # Get self in raw Pulp form.
        self_raw = self._to_data()

        # Make a filtered view keeping only mutable note fields.
        out = {}

        for field in self._mutable_note_fields():
            pulp_field = field.metadata.get(PULP2_FIELD)
            pulp_value = lookup(self_raw, pulp_field)

            if pulp_value is not ABSENT:
                dict_put(out, pulp_field, pulp_value)

        # Return only the notes portion.
        return out.get("notes") or {}

    def distributor(self, distributor_id):
        """Look up a distributor by ID.

        Returns:
            :class:`~pubtools.pulplib.Distributor`
                The distributor belonging to this repository with the given ID.
            None
                If this repository has no distributor with the given ID.
        """
        return self._distributors_by_id.get(distributor_id)

    @property
    def file_content(self):
        """A list of file units stored in this repository.

        Returns:
            list[:class:`~pubtools.pulplib.FileUnit`]

        .. versionadded:: 2.4.0
        """
        return list(self.search_content(Criteria.with_field("content_type_id", "iso")))

    @property
    def rpm_content(self):
        """A list of rpm units stored in this repository.

        Returns:
            list[:class:`~pubtools.pulplib.RpmUnit`]

        .. versionadded:: 2.4.0
        """
        return list(self.search_content(Criteria.with_field("content_type_id", "rpm")))

    @property
    def srpm_content(self):
        """A list of srpm units stored in this repository.

        Returns:
            list[:class:`~pubtools.pulplib.Unit`]

        .. versionadded:: 2.4.0
        """
        return list(self.search_content(Criteria.with_field("content_type_id", "srpm")))

    @property
    def modulemd_content(self):
        """A list of modulemd units stored in this repository.

        Returns:
            list[:class:`~pubtools.pulplib.ModulemdUnit`]

        .. versionadded:: 2.4.0
        """
        return list(
            self.search_content(Criteria.with_field("content_type_id", "modulemd"))
        )

    @property
    def modulemd_defaults_content(self):
        """A list of modulemd_defaults units stored in this repository.

        Returns:
            list[:class:`~pubtools.pulplib.ModulemdDefaultsUnit`]

        .. versionadded:: 2.4.0
        """
        return list(
            self.search_content(
                Criteria.with_field("content_type_id", "modulemd_defaults")
            )
        )

    def search_content(self, criteria=None):
        """Search this repository for content matching the given criteria.

        Args:
            criteria (:class:`~pubtools.pulplib.Criteria`)
                A criteria object used for this search.

        Returns:
            Future[:class:`~pubtools.pulplib.Page`]
                A future representing the first page of results.

                Each page will contain a collection of
                :class:`~pubtools.pulplib.Unit` objects.

        .. versionadded:: 2.4.0
        """
        if not self._client:
            raise DetachedException()

        return self._client._search_repo_units(self.id, criteria)

    def delete(self):
        """Delete this repository from Pulp.

        Returns:
            Future[list[:class:`~pubtools.pulplib.Task`]]
                A future which is resolved when the repository deletion has completed.

                The future contains a list of zero or more tasks triggered and awaited
                during the delete operation.

                This object also becomes detached from the client; no further updates
                are possible.

        Raises:
            DetachedException
                If this instance is not attached to a Pulp client.
        """
        return self._delete("repositories", self.id)

    def publish(self, options=PublishOptions()):
        """Publish this repository.

        The specific operations triggered on Pulp in order to publish a repo are not defined,
        but in Pulp 2.x, generally consists of triggering one or more distributors in sequence.

        Args:
            options (PublishOptions)
                Options used to customize the behavior of this publish.

                If omitted, the Pulp server's defaults apply.

        Returns:
            Future[list[:class:`~pubtools.pulplib.Task`]]
                A future which is resolved when publish succeeds.

                The future contains a list of zero or more tasks triggered and awaited
                during the publish operation.

        Raises:
            DetachedException
                If this instance is not attached to a Pulp client.
        """
        if not self._client:
            raise DetachedException()

        # Before adding distributors and publishing, we'll activate this hook
        # to allow subscribing implementers the opportunity to adjust options.
        hook_rets = pm.hook.pulp_repository_pre_publish(
            repository=self, options=options
        )
        # Use the first non-None hook return value to replace options.
        hook_rets = [ret for ret in hook_rets if ret is not None]
        options = hook_rets[0] if hook_rets else options

        # All distributor IDs we're willing to invoke. Anything else is ignored.
        # They'll be invoked in the order listed here.
        candidate_distributor_ids = self._PUBLISH_DISTRIBUTORS

        to_publish = []

        for candidate in candidate_distributor_ids:
            distributor = self._distributors_by_id.get(candidate)
            if not distributor:
                # nothing to be done
                continue

            if (
                distributor.id == "docker_web_distributor_name_cli"
                and options.origin_only
            ):
                continue

            config = self._config_for_distributor(distributor, options)
            to_publish.append((distributor, config))

        out = self._client._publish_repository(self, to_publish)

        def do_published_hook(tasks):
            # Whenever we've published successfully, we'll activate this hook
            # before returning.
            pm.hook.pulp_repository_published(repository=self, options=options)
            return tasks

        out = f_map(out, do_published_hook)
        return f_proxy(out)

    def sync(self, options=None):
        """Sync repository with feed.

        Args:
            options (SyncOptions)
                Options used to customize the behavior of sync process.
                If omitted, the Pulp server's defaults apply.

        Returns:
            Future[list[:class:`~pubtools.pulplib.Task`]]
                A future which is resolved when sync succeeds.

                The future contains a list of zero or more tasks triggered and awaited
                during the sync operation.

        Raises:
            DetachedException
                If this instance is not attached to a Pulp client.

        .. versionadded:: 2.5.0
        """
        options = options or SyncOptions(feed="")

        if not self._client:
            raise DetachedException()

        return f_proxy(
            self._client._do_sync(
                self.id, asdict(options, filter=lambda name, val: val is not None)
            )
        )

    def lock(self, context, duration=None):
        """
        Obtain an exclusive advisory lock on this repository.

        Returns a context manager representing the lock, intended to be used
        via a `with` statement. When the context is entered, the caller will
        wait until the lock can be acquired (or raise an exception if the lock
        can't be acquired).

        Only a single :class:`~pubtools.pulplib.Client` is able to hold the lock
        on a repository at any given time. The lock does not prevent modifications
        to the repo with the Pulp API, and does not affect other Pulp client
        implementations or instances of :class:`~pubtools.pulplib.Client` not
        using the `lock` method.

        Args:
            context:
                A short description of the task being carried out with the lock.

                This value will be added to the lock in the repo and may be
                used for debugging.

            duration
                Maximum duration of the lock, in seconds.

                This value is used only if this client fails to release the
                lock (for example, because the current process is killed).
                In this case, the duration will be used by other clients in
                order to detect and release stale locks, avoiding a deadlock.

                There is no way to extend the duration of an acquired lock,
                so the caller should always ensure they request a `duration`
                high enough to cover the entire expected lifetime of the lock.
        """

        return RepoLock(self.id, self._client, context, duration)

    def remove_content(self, criteria=None, **kwargs):
        """Remove all content of requested types from this repository.

        Args:
            criteria (:class:`~pubtools.pulplib.Criteria`)
                A criteria object used to filter the contents for removal.

                Type IDs must be included in the criteria with any other filters.
                If omitted, filter criteria will be ignored and all the content will
                be removed.
                If criteria is omitted, all the content will be removed.

        Returns:
            Future[list[:class:`~pubtools.pulplib.Task`]]
                A future which is resolved when content has been removed.

                The future contains a list of zero or more tasks triggered and awaited
                during the removal.

                To obtain information on the removed content, use
                :meth:`~pubtools.pulplib.Task.units`.

        Raises:
            DetachedException
                If this instance is not attached to a Pulp client.

        .. versionadded:: 1.5.0
        """
        if not self._client:
            raise DetachedException()

        # Type IDs are must for the criteria filter to be effective. It must be included
        # in the criteria. Type IDs provided as type_id kwargs will be ignored for the
        # criteria and will remove all the content in the repo.
        # Refer to https://bugzilla.redhat.com/show_bug.cgi?id=1021579 and Pulp
        # documentation for more details.

        # Note: type_ids is deprecated. Criteria.with_unit_type should be used to
        # filter on type_ids. This is kept for backward compatibility and will be
        # removed in future versions.
        if not criteria:
            type_ids = kwargs.get("type_ids")
            # use content_type_id field name to coerce
            # search_for_criteria to fill out the PulpSearch#type_ids field.
            # passing a criteria with an empty type_ids list rather than
            # None results in failing tests due to the implementation of
            # FakeClient#_do_unassociate
            if type_ids is not None:
                warnings.warn(
                    "type_ids is deprecated, use criteria instead", DeprecationWarning
                )
                criteria = Criteria.with_field(
                    "content_type_id",
                    Matcher.in_(type_ids),  # Criteria.with_field_in is deprecated
                )

        return f_proxy(self._client._do_unassociate(self.id, criteria=criteria))

    @classmethod
    def from_data(cls, data):
        # delegate to concrete subclass as needed
        if cls is Repository:
            notes = data.get("notes") or {}
            for notes_type, klass in REPO_CLASSES.items():
                if notes.get("_repo-type") == notes_type:
                    return klass.from_data(data)

        return super(Repository, cls).from_data(data)

    @classmethod
    def _data_to_init_args(cls, data):
        out = super(Repository, cls)._data_to_init_args(data)

        for dist in data.get("distributors") or []:
            if dist["distributor_type_id"] in ("yum_distributor", "iso_distributor"):
                out["relative_url"] = (dist.get("config") or {}).get("relative_url")

            if dist["id"] == "cdn_distributor":
                skip_repodata = (dist.get("config") or {}).get("skip_repodata")
                if skip_repodata is not None:
                    out["skip_rsync_repodata"] = skip_repodata

        return out

    @classmethod
    def _config_for_distributor(cls, distributor, options):
        out = {}

        if distributor.is_rsync:
            if options.clean is not None:
                out["delete"] = options.clean
            if options.origin_only is not None:
                out["content_units_only"] = options.origin_only
            if options.rsync_extra_args is not None:
                out["rsync_extra_args"] = options.rsync_extra_args

        if options.force is not None:
            out["force_full"] = options.force

        return out

    def _set_client(self, client):
        super(Repository, self)._set_client(client)

        # distributors use the same client as owning repository
        for distributor in self.distributors or []:
            distributor._set_client(client)

    def _upload_then_import(
        self, file_obj, name, type_id, unit_key_fn=None, unit_metadata_fn=None
    ):
        """Private helper to upload and import a piece of content into this repo.

        To be called by the type-specific subclasses (e.g. YumRepository,
        FileRepository...)

        Args:
            file_obj (str, file-like object, None):
                file object or path (as documented in public methods), or None
                if this unit type has no associated file

            name (str):
                a brief user-meaningful name for the content being uploaded
                (appears in logs)

            type_id (str):
                pulp unit type ID

            unit_key_fn (callable):
                a callable which will be invoked with the return value of
                _do_upload_file (or None if file_obj is None).
                It should return the unit key for this piece of
                content. If omitted, an empty unit key is used, which means Pulp
                is wholly responsible for calculating the unit key.

            unit_metadata_fn (callable):
                a callable which will be invoked with the return value of
                _do_upload_file (or None if file_obj is None). It should return
                the unit metadata for this piece of
                content. If omitted, metadata is not included in the import call to
                Pulp.
        """

        if not self._client:
            raise DetachedException()

        unit_key_fn = unit_key_fn or (lambda _: {})
        unit_metadata_fn = unit_metadata_fn or (lambda _: None)

        upload_id_f = f_map(
            self._client._request_upload(name), lambda upload: upload["upload_id"]
        )

        f_map(
            upload_id_f,
            lambda upload_id: LOG.info(
                "Uploading %s to %s [%s]", name, self.id, upload_id
            ),
        )

        if file_obj is None:
            # If there is no file for this kind of unit (e.g. erratum),
            # we still have to use the request_upload and import APIs; we just
            # never upload any bytes. That means the upload is 'complete' as
            # soon as the upload ID is known. A real upload returns a (size, checksum)
            # tuple; we force a no-content upload to return None.
            upload_complete_f = f_map(upload_id_f, lambda _: None)
        else:
            upload_complete_f = f_flat_map(
                upload_id_f,
                lambda upload_id: self._client._do_upload_file(
                    upload_id, file_obj, name
                ),
            )

        import_complete_f = f_flat_map(
            upload_complete_f,
            lambda upload: self._client._do_import(
                self.id,
                upload_id_f.result(),
                type_id,
                unit_key_fn(upload),
                unit_metadata_fn(upload),
            ),
        )

        f_map(
            import_complete_f,
            lambda _: self._client._delete_upload_request(upload_id_f.result(), name),
        )

        return f_proxy(import_complete_f)
