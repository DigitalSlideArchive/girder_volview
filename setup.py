from setuptools import setup, find_packages

with open("README.md") as readme_file:
    readme = readme_file.read()

requirements = [
    "girder>=3.0.0a1",
    "pyyaml",
]

setup(
    author="Paul Elliott",
    author_email="paul.elliott@kitware.com",
    classifiers=[
        "Development Status :: 2 - Pre-Alpha",
        "License :: OSI Approved :: Apache Software License",
        "Natural Language :: English",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
    ],
    description="Open items in VolView.",
    install_requires=requirements,
    license="Apache Software License 2.0",
    long_description=readme,
    long_description_content_type="text/markdown",
    include_package_data=True,
    keywords="girder-plugin, volview",
    name="girder_volview",
    packages=find_packages(exclude=["test", "test.*"]),
    url="https://github.com/PaulHax/girder_volview",
    zip_safe=False,
    entry_points={"girder.plugin": ["volview = girder_volview:GirderPlugin"]},
    setup_requires=["setuptools_scm"],
    use_scm_version={"fallback_version": "0.1.1"},
)
