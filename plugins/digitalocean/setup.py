import os
from setuptools import setup, find_packages

with open("requirements.txt") as f:
    requirements = f.read().splitlines()


def read(fname):
    return open(os.path.join(os.path.dirname(__file__), fname)).read()


setup(
    name="resoto-plugin-digitalocean",
    version="2.0.0a22",
    description="Resoto DigitalOcean Collector Plugin",
    license="Apache 2.0",
    packages=find_packages(),
    long_description=read("README.md"),
    entry_points={
        "resoto.plugins": [
            "digitalocean_collector = resoto_plugin_digitalocean:DigitalOceanCollectorPlugin"
        ]
    },
    include_package_data=True,
    zip_safe=False,
    install_requires=requirements,
    setup_requires=["pytest-runner"],
    tests_require=["pytest"],
    classifiers=[
        # Current project status
        "Development Status :: 4 - Beta",
        # Audience
        "Intended Audience :: System Administrators",
        "Intended Audience :: Information Technology",
        # License information
        "License :: OSI Approved :: Apache Software License",
        # Supported python versions
        "Programming Language :: Python :: 3.8",
        # Supported OS's
        "Operating System :: POSIX :: Linux",
        "Operating System :: Unix",
        # Extra metadata
        "Environment :: Console",
        "Natural Language :: English",
        "Topic :: Security",
        "Topic :: Utilities",
    ],
    keywords="cloud security",
)
