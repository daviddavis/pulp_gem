import asyncio
import logging
import os

from gettext import gettext as _
from urllib.parse import urlparse, urlunparse

from pulpcore.plugin.models import Artifact, ProgressBar, Repository, RepositoryVersion
from pulpcore.plugin.stages import (
    DeclarativeArtifact,
    DeclarativeContent,
    DeclarativeVersion,
    Stage,
    ArtifactDownloader,
    ArtifactSaver,
    QueryExistingContentUnits,
    ContentUnitSaver,
    ContentUnitAssociation,
    ContentUnitUnassociation,
    EndStage,
    create_pipeline,
)

from pulpcore.plugin.tasking import WorkingDirectory

from pulp_gem.app.models import GemContent, GemRemote
from pulp_gem.specs import read_specs


log = logging.getLogger(__name__)


class ExistingContentNeedsNoArtifacts(Stage):
    """
    Stage to remove declarative_artifacts from existing content.

    A Stages API stage that removes all
    :class:`~pulpcore.plugin.stages.DeclarativeArtifact` instances from
    :class:`~pulpcore.plugin.stages.DeclarativeContent` units if the respective
    :class:`~pulpcore.plugin.models.Content` is already existing.
    """

    async def __call__(self, in_q, out_q):
        """
        The coroutine for this stage.

        Args:
            in_q (:class:`asyncio.Queue`): The queue to receive
                :class:`~pulpcore.plugin.stages.DeclarativeContent` objects from.
            out_q (:class:`asyncio.Queue`): The queue to put
                :class:`~pulpcore.plugin.stages.DeclarativeContent` into.

        Returns:
            The coroutine for this stage.

        """
        async for batch in self.batches(in_q):
            for declarative_content in batch:
                if declarative_content.content.pk is not None:
                    declarative_content.d_artifacts = []
                await out_q.put(declarative_content)
        await out_q.put(None)


def synchronize(remote_pk, repository_pk, mirror):
    """
    Create a new version of the repository that is synchronized with the remote as specified.

    Args:
        remote_pk (str): The remote PK.
        repository_pk (str): The repository PK.
        mirror (bool): True for mirror mode, False for additive.

    Raises:
        ValueError: If the remote does not specify a URL to sync.

    """
    remote = GemRemote.objects.get(pk=remote_pk)
    repository = Repository.objects.get(pk=repository_pk)

    if not remote.url:
        raise ValueError(_('A remote must have a url specified to synchronize.'))

    first_stage = GemFirstStage(remote)
    GemDeclarativeVersion(first_stage, repository, mirror).create()


class GemFirstStage(Stage):
    """
    The first stage of a pulp_gem sync pipeline.
    """

    def __init__(self, remote):
        """
        The first stage of a pulp_gem sync pipeline.

        Args:
            remote (GemRemote): The remote data to be used when syncing

        """
        self.remote = remote

    async def __call__(self, in_q, out_q):
        """
        Build and emit `DeclarativeContent` from the Spec data.

        Args:
            in_q (asyncio.Queue): Unused because the first stage doesn't read from an input queue.
            out_q (asyncio.Queue): The out_q to send `DeclarativeContent` objects to

        """
        with ProgressBar(message='Downloading Metadata') as pb:
            parsed_url = urlparse(self.remote.url)
            root_dir = parsed_url.path
            specs_path = os.path.join(root_dir, 'specs.4.8.gz')
            specs_url = urlunparse(parsed_url._replace(path=specs_path))
            downloader = self.remote.get_downloader(url=specs_url)
            result = await downloader.run()
            pb.increment()

        with ProgressBar(message='Parsing Metadata') as pb:
            for key in read_specs(result.path):
                relative_path = os.path.join('gems', key.name + '-' + key.version + '.gem')
                path = os.path.join(root_dir, relative_path)
                url = urlunparse(parsed_url._replace(path=path))

                spec_relative_path = os.path.join('quick/Marshal.4.8',
                                                  key.name + '-' + key.version + '.gemspec.rz')
                spec_path = os.path.join(root_dir, spec_relative_path)
                spec_url = urlunparse(parsed_url._replace(path=spec_path))
                gem = GemContent(name=key.name, version=key.version)
                da_gem = DeclarativeArtifact(Artifact(), url, relative_path, self.remote)
                da_spec = DeclarativeArtifact(Artifact(), spec_url, spec_relative_path, self.remote)
                dc = DeclarativeContent(content=gem, d_artifacts=[da_gem, da_spec])
                pb.increment()
                await out_q.put(dc)
        await out_q.put(None)


class GemDeclarativeVersion(DeclarativeVersion):
    """
    Custom implementation of Declarative version.

    This should go away with the new plugin api.
    """

    def create(self):
        """
        Perform the work. This is the long-blocking call where all syncing occurs.
        """
        with WorkingDirectory():
            with RepositoryVersion.create(self.repository) as new_version:
                loop = asyncio.get_event_loop()
                stages = [
                    self.first_stage,
                    QueryExistingContentUnits(),
                    ExistingContentNeedsNoArtifacts(),
                    ArtifactDownloader(),
                    ArtifactSaver(),
                    ContentUnitSaver(),
                    ContentUnitAssociation(new_version)
                ]
                if self.mirror:
                    stages.append(ContentUnitUnassociation(new_version))
                stages.append(EndStage())
                pipeline = create_pipeline(stages)
                loop.run_until_complete(pipeline)
