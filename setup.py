#!/usr/bin/env python3

from setuptools import setup

with open('README.md') as readme_file:
    readme = readme_file.read()


setup(
    author="James O'Beirne",
    author_email='james.obeirne@pm.me',
    python_requires='>=3.7',
    classifiers=[
        'Intended Audience :: Developers',
        'License :: OSI Approved :: MIT License',
        'Natural Language :: English',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.7',
        'Programming Language :: Python :: 3.8',
        'Programming Language :: Python :: 3.9',
    ],
    description="tools for reviewing Bitcoin Core",
    license="MIT license",
    include_package_data=True,
    long_description=readme,
    long_description_content_type='text/markdown',
    keywords='ackr',
    name='ackr',
    py_modules=['ackr'],
    install_requires=[
        'clii',
    ],
    entry_points={
        'console_scripts': [
            'ackr=ackr:main',
        ],
    },
    url='https://github.com/jamesob/ackr',
    version='0.1.0',
)
