#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2020-2022 Wojtek Porczyk <woju@invisiblethingslab.com>

from dataclasses import dataclass

import collections.abc
import dataclasses
import datetime
import pathlib
import pdb
import sys
import time

import click
import dateutil.parser
import httpx
import jinja2
import tomli

# Do not use packaging.version.Version! Rationale:
#   packaging.version.InvalidVersion: Invalid version: '13.3.3~bpo10+1+apertis2'
# Instead, prefer https://pypi.org/project/looseversion/, however in Debian
# it's available since trixie, so we'll use distutil's LooseVersion and ignore
# the warning.
try:
    from looseversion import LooseVersion
except ImportError:
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)
        from distutils.version import LooseVersion

env = jinja2.Environment(loader=jinja2.FileSystemLoader('templates'))


def underscorify(d):
    return {k.replace('-', '_'): v for k, v in d.items()}

def parse_date(date):
    # For partial dates, like '2021' or '2021-04' this should return last
    # possible day, because if the date is partial, it's probably unreleased,
    # so we'd like to compare it later than anything specific.
    # Fortunately (..., 31) works even for shorter months.

    if date is None or isinstance(date, datetime.date):
        return date

    return dateutil.parser.parse(date, default=datetime.date(1970, 12, 31))

# http://stackoverflow.com/a/3233356
# cf. salt/utils/dictupdate.py
def update(d, u):
    for k, v in u.items():
        if isinstance(v, collections.abc.Mapping):
            d[k] = update(d.get(k, {}), v)
        else:
            d[k] = v
    return d

@dataclass
class Distro:
    distro: str
    version: str

    repo: str
    codename: str = None
    backports: str = None

    # those are str, to allow both sorting and partial specification
    released: str = None
    eol: str = None
    lts: str = None

    @staticmethod
    def datadate(date):
        if isinstance(date, int):
            return str(date)
        if date is None or isinstance(date, (str, datetime.date)):
            return date
        raise TypeError()

    @classmethod
    def fromdata(cls, distro, version, *, released=None, eol=None, lts=None, **kwds):
        released = cls.datadate(released)
        eol = cls.datadate(eol)
        lts = cls.datadate(lts)
        return cls(distro=distro, version=version, released=released, eol=eol, lts=lts, **kwds)

    def __lt__(self, other):
        if self.eol is None:
            return True
        if other.eol is None:
            return False
        if isinstance(self.eol, str) and self.eol.casefold() == 'yolo':
            return True
        if isinstance(other.eol, str) and other.eol.casefold() == 'yolo':
            return False
        return ((parse_date(self.eol), self.distro, LooseVersion(self.version))
            < (parse_date(other.eol), other.distro, LooseVersion(other.version)))

    def __hash__(self):
        return hash((self.distro, self.version, self.codename))

    def is_released(self, now):
        return self.released is None or parse_date(self.released) <= now.date()

    def is_eol(self, now):
        return self.eol is not None and (
            self.eol == 'yolo' or parse_date(self.eol) < now.date())


@dataclass
class Package:
    id: str
    min: LooseVersion = None
    max: LooseVersion = None
    alt_names: list = None

    min_found: str = dataclasses.field(default=None, repr=False)

    def __hash__(self):
        return hash(self.id)

    @property
    def all_ids(self):
        yield self.id
        if self.alt_names:
            yield from self.alt_names


@dataclass
class PackageVersion:
    distro: Distro
    package: Package

    version:  LooseVersion = None
    backport: LooseVersion = None

    @property
    def is_lowest(self):
        return self.version is not None and self.version == self.package.min_found

    def update(self, pkglist):
        for pkg in pkglist:
            version = LooseVersion(pkg['version'].replace('-', '.'))
            if pkg['repo'] == self.distro.repo:
#               print(f'{self.package=} {pkg=} {version=} {self.version=}', file=sys.stderr)
                self.version = version if self.version is None else max(version, self.version)
                continue
            if pkg['repo'] == self.distro.backports:
                self.backport = version if self.backport is None else max(version, self.backport)
                continue


@click.command()
@click.argument('file', type=click.File('rb'), nargs=-1)
def main(file):
    data = {}
    if not file:
        file = [open(path, 'rb') for path in pathlib.Path('.').glob('data/*.toml')]
    for fd in file:
        with fd:
            update(data, tomli.load(fd))

    distros = []
    for distro, versions in data['distro'].items():
        for version, distrodata in versions.items():
            distros.append(Distro.fromdata(distro, version, **distrodata))
    distros.sort(reverse=True)

    packages = []
    for package, packagedata in data['package'].items():
        packages.append(Package(package, **underscorify(packagedata)))

    lines = {
        distro: {package: PackageVersion(distro, package) for package in packages}
        for distro in distros}

    with httpx.Client() as http:
        for package in packages:
            print(f'package={package}', file=sys.stderr)
            for pkgid in reversed(list(package.all_ids)):
                delay = 1
                while True:
                    try:
                        resp = http.get(f'https://repology.org/api/v1/projects/{pkgid}/')
                        resp.raise_for_status()
                        pkglist = resp.json()[pkgid]
                        break
                    except httpx.ReadTimeout:
                        print(f'  read timeout, delaying {delay} s', file=sys.stderr)
                        time.sleep(delay)
                        delay *= 1.5

                for line in lines.values():
                    for p, pv in line.items():
                        if p is not package:
                            continue
                        pv.update(pkglist)

                for line in lines.values():
                    v = line[package].version
                    if v is not None and package.min is not None and v >= package.min:
                        package.min_found = (
                            v if package.min_found is None
                            else min(v, package.min_found))

    print(env.get_template('distro-versions.html').render(
        distros=distros,
        packages=packages,
        lines=lines,
        now=datetime.datetime.utcnow().replace(tzinfo=datetime.timezone.utc),
    ), end='')


if __name__ == '__main__':
    main()
