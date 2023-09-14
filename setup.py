"""Python setup.py for i3expo package"""
import io
import os
from setuptools import find_packages, setup


# note the version is managed by zest.releaser:
version = "0.0.1.dev0"


def read(*paths, **kwargs):
    """Read the contents of a text file safely.
    >>> read("README.md")
    ...
    """

    content = ""
    with io.open(
        os.path.join(os.path.dirname(__file__), *paths),
        encoding=kwargs.get("encoding", "utf8"),
    ) as open_file:
        content = open_file.read().strip()
    return content


def read_requirements(path):
    return [
        line.strip()
        for line in read(path).split("\n")
        if not line.startswith(('"', "#", "-", "git+"))
    ]


setup(
    name="i3expo",
    version=version,
    description="Awesome i3expo created by laur89",
    url="https://github.com/laur89/i3expo",
    long_description=read("README.md"),
    long_description_content_type="text/markdown",
    author="laur89",
    # packages=find_packages(exclude=["tests", ".github", "img"]),
    packages=["i3expo"],
    install_requires=read_requirements("requirements.txt"),
    entry_points={
        "console_scripts": ["i3expo = i3expo.__main__:run"]
        #"console_scripts": ["i3expo = i3expo:run"]
    },
    extras_require={"test": read_requirements("requirements-test.txt")},
)
