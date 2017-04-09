#!/usr/bin/env python3

# Copyright (C) 2017 John Koelndorfer
#
# This file is part of mccdl.
#
# mccdl is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.

# mccdl is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with mccdl.  If not, see <http://www.gnu.org/licenses/>.

import argparse
from collections import namedtuple
from distutils.dir_util import copy_tree
import errno
from functools import reduce
import hashlib
import json
import logging
import os
from pathlib import Path
from urllib.parse import unquote as urlunquote, urljoin as _urljoin
import shutil
import sys
import textwrap
import zipfile

import appdirs
import requests


CurseForgeModPackFile = namedtuple("CurseForgeModPackFile", ("project_id", "file_id", "required"))


def logger(obj):
    """
    Returns a logger suitable for use by the passed in object.
    """
    return logging.getLogger(".".join(["mccdl", obj.__class__.__name__]))


def urljoin(base, *parts):
    """
    URL join function that makes more sense than Python's standard library version.
    """
    return reduce(lambda base, part: _urljoin(base + "/", str(part).lstrip("/")), parts, base)


class CurseForgeClient:
    CURSE_BASE_URL = "http://minecraft.curseforge.com"
    DEFAULT_CACHE_DIR = Path(appdirs.user_cache_dir("mccdl"))

    def __init__(self, instance_manager, downloader, unpacker):
        self.downloader = downloader
        self.instance_manager = instance_manager
        self.logger = logger(self)
        self.unpacker = unpacker

    def install_modpack(self, project_id, instance_name, file_id="latest"):
        self.logger.info("Installing modpack %s to instance %s, file ID %s",
                         str(project_id), instance_name, str(file_id))
        modpack_extract_dir = self.project(project_id).download_and_unpack_file(file_id)
        modpack = CurseForgeModPack(modpack_extract_dir)

        instance = self.instance_manager.instance(instance_name)
        instance.create(modpack.minecraft_version, modpack.forge_version)

        for modpack_file in modpack.files():
            self.project(modpack_file.project_id).download_file(
                modpack_file.file_id, instance.mods_directory
            )
        modpack.install_overrides(instance.minecraft_directory)

    def project(self, project_id):
        return CurseForgeProject(self, project_id)

    def url_for(self, *path):
        return urljoin(self.CURSE_BASE_URL, *path)


class CurseForgeProject:
    def __init__(self, client, project_id):
        self._client = client
        self.logger = logger(self)
        self.project_id = project_id

    def download_and_unpack_file(self, file_id):
        archive_path = self.download_file(file_id)
        unpack_directory = self._client.unpacker.unpack(archive_path)
        return unpack_directory

    def download_file(self, file_id, destination=None):
        self.logger.debug("Downloading project %s, file %s", str(self.project_id), str(file_id))
        return self._client.downloader.download(self.file_url(file_id), destination)

    def file_url(self, file_id):
        url_parts = ["files", file_id]
        if file_id != "latest":
            url_parts.append("download")
        url = self.url_for(*url_parts)
        self.logger.debug("URL for project %s, file %s is %s", self.project_id, file_id, url)
        return url

    def url_for(self, *path):
        return self._client.url_for("projects", self.project_id, *path)


class CurseForgeDownloadUnpacker:
    def __init__(self, unpack_dir):
        self.logger = logger(self)
        self.unpack_dir = Path(unpack_dir)

    def unpack(self, archive_path):
        self.logger.debug("Unpacking archive %s", archive_path)
        unpack_destination = self._unpack_destination(archive_path)
        try:
            shutil.rmtree(unpack_destination)
        except OSError as e:
            if e.errno != errno.ENOENT:
                raise e
        zipf = zipfile.ZipFile(str(archive_path))
        zipf.extractall(unpack_destination)
        self.logger.debug("Unpacked archive to %s", unpack_destination)

        return unpack_destination

    def _unpack_destination(self, archive_path):
        return self.unpack_dir / os.path.basename(archive_path)


class CurseForgeModPack:
    def __init__(self, unpack_directory):
        self.unpack_directory = Path(unpack_directory)
        with open(self.unpack_directory / "manifest.json", "r") as f:
            self.manifest = json.loads(f.read())

    def files(self):
        for i in self.manifest["files"]:
            yield CurseForgeModPackFile(i["projectID"], i["fileID"], i["required"])

    def install_overrides(self, destination):
        # FIXME: distutils.dir_util.copy_tree seems to keep some internal state when it does its copy.
        #
        # If the destination directory disappears, copy_tree will not recreate missing path
        # components.
        #
        # In practice this should not be an issue since our script will execute once to install a
        # modpack, then exit.
        copy_tree(str(self.unpack_directory / self.manifest["overrides"]), str(destination))

    @property
    def forge_version(self):
        # TODO: Make this less brittle.
        #
        # The Forge modloader ID looks like "forge-12.18.3.2254", so strip off the
        # leading "forge-".
        return self.manifest["minecraft"]["modLoaders"][0]["id"].replace("forge-", "")

    @property
    def minecraft_version(self):
        return self.manifest["minecraft"]["version"]


class CachingDownloader:
    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self.logger = logger(self)

    def download(self, url, destination=None):
        url_cache_path = self._path_for_url(url)
        self.logger.debug("Cache directory for %s is %s", url, url_cache_path)
        cached_file_path = None
        if os.path.exists(url_cache_path):
            self.logger.debug("Cache directory for %s already exists", url)
            cached_dir_content = os.listdir(url_cache_path)
            cached_file_path = (url_cache_path / cached_dir_content[0]) if cached_dir_content else None

        if cached_file_path is None:
            self.logger.debug("No cached download for %s, downloading", url)
            cached_file_path = self._download(url)

        if destination is not None:
            self.logger.debug("Copying cached file %s to %s", cached_file_path, destination)
            destination = Path(destination)
            self._mkdir_p(destination.parent)
            shutil.copy(cached_file_path, destination)
            download_destination = destination
        else:
            download_destination = cached_file_path

        return download_destination

    def _download(self, url):
        url_cache_path = self._path_for_url(url)

        response = requests.get(url, stream=True)
        response.raise_for_status()
        download_destination = self._download_destination(url_cache_path, response.url)

        self._mkdir_p(download_destination.parent)

        with open(download_destination, "wb") as f:
            for buf in response.iter_content(1024):
                f.write(buf)

        return download_destination

    def _mkdir_p(self, path):
        try:
            os.makedirs(path)
        except OSError as e:
            if not e.errno == errno.EEXIST:
                raise e

    def _url_digest(self, url):
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def _path_for_url(self, url):
        return self.cache_dir / self._url_digest(url)

    @classmethod
    def _download_filename(cls, response_url):
        return urlunquote(response_url.split("/")[-1])

    def _download_destination(self, dir_path, url):
        return dir_path / self._download_filename(url)


class MccdlCommandLineApplication:
    def __init__(self):
        self.argparser = argparse.ArgumentParser()
        self.configure_argparser()

    def configure_argparser(self):
        a = self.argparser
        a.add_argument(
            "-c", "--cache-directory", type=str, default=str(CurseForgeClient.DEFAULT_CACHE_DIR),
            help="Path to directory to cache mccdl files. Defaults to %(default)s."
        )
        a.add_argument(
            "--multimc-directory", type=str, default=appdirs.user_data_dir("multimc5"),
            help="Path to the MultiMC directory. Defaults to %(default)s."
        )
        a.add_argument(
            "--modpack-file-id", type=str, default="latest",
            help="File ID of the modpack to download. Defaults to %(default)s."
        )
        a.add_argument(
            "modpack_name", type=str,
            help="ID or name of the modpack to download."
        )
        a.add_argument(
            "instance_name", type=str,
            help="Name of the MultiMC instance to create."
        )

    def make_curseforge_client(self, args):
        cache_dir = Path(args.cache_directory)
        downloader = CachingDownloader(cache_dir / "download")
        unpacker = CurseForgeDownloadUnpacker(cache_dir / "unpack")
        instance_manager = MultiMcInstanceManager(args.multimc_directory, downloader)

        return CurseForgeClient(instance_manager, downloader, unpacker)

    def run(self, argv):
        args = self.argparser.parse_args(argv)
        curseforge_client = self.make_curseforge_client(args)

        curseforge_client.install_modpack(args.modpack_name, args.instance_name, args.modpack_file_id)


class MultiMcInstanceManager:
    def __init__(self, multimc_directory, downloader):
        self.multimc_directory = Path(multimc_directory)
        self.downloader = downloader

    def create(self, instance_name, minecraft_version, forge_version):
        instance = self.instance(instance_name)
        instance.create(minecraft_version, forge_version)

        return instance

    def instance(self, name):
        return MultiMcInstance(self._instance_dir(name), name, self)

    def _instance_dir(self, instance_name):
        return self.multimc_directory / "instances" / instance_name


class MultiMcInstance:
    MULTIMC_FORGE_CONFIGURATION_SITE = "https://meta.multimc.org/net.minecraftforge"

    def __init__(self, directory, name, instance_manager):
        self.directory = Path(directory)
        self.logger = logger(self)
        self.name = name
        self.instance_manager = instance_manager

    def create(self, minecraft_version, forge_version):
        self.logger.info("Creating MultiMC instance %s, Minecraft version %s, Forge version %s",
                         self.name, minecraft_version, forge_version)
        os.makedirs(self.minecraft_directory)
        os.makedirs(self.mods_directory)
        self._configure_instance_base(minecraft_version)
        self._configure_instance_forge(minecraft_version, forge_version)

    def _configure_instance_base(self, minecraft_version):
        instance_cfg = textwrap.dedent("""
            InstanceType=OneSix
            IntendedVersion={minecraft_version}
            iconKey=default
            name={instance_name}
        """).format(minecraft_version=minecraft_version, instance_name=self.name).lstrip()
        instance_cfg_path = self.directory / "instance.cfg"
        self.logger.debug("Wrote instance configuration to %s", instance_cfg_path)
        with open(instance_cfg_path, "w") as f:
            f.write(instance_cfg)

    def _configure_instance_forge(self, minecraft_version, forge_version):
        self.logger.debug("Configuring MultiMC instance Forge")
        patches_dir = self.directory / "patches"
        self.instance_manager.downloader.download(
            self._forge_config_url(minecraft_version, forge_version),
            patches_dir / "net.minecraftforge.json"
        )

    def _forge_config_url(self, minecraft_version, forge_version):
        forge_config_filename = "{}-{}.json".format(minecraft_version, forge_version)
        return urljoin(self.MULTIMC_FORGE_CONFIGURATION_SITE, forge_config_filename)

    @property
    def minecraft_directory(self):
        return self.directory / "minecraft"

    @property
    def mods_directory(self):
        return self.minecraft_directory / "mods"


if __name__ == "__main__":
    MccdlCommandLineApplication().run(sys.argv[1:])
