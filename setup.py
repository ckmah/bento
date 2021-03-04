from setuptools import setup

setup(name='bento',
      version='0.1',
      description='Spatial RNA analysis toolkit',
      url='http://github.com/ckmah/bento',
      author='Clarence Mah',
      author_email='ckmah@ucsd.edu',
      license='MIT',
      packages=['bento'],
      python_requires='>3.5, <3.8',
      include_package_data=True,
      install_requires=['anndata>=0.7.1',
                        'altair==3.3.0',
                        'descartes==1.1.0',
                        'geopandas>=0.8.0',
                        'ipywidgets>=7.5.1',
                        'leidenalg>=0.8.3',
                        'matplotlib>=3.2.1',
                        'numpy>=1.18.4',
                        'opencv-python>=4.4.0',
                        'optuna>=2.3.0',
                        'pandarallel>=1.5.2',
                        'pandas>=1.2.0',
                        'pygeos>=0.8',
                        'seaborn>=0.10.1',
                        'scanpy>=1.6.0',
                        'scikit-learn>=0.22.2.post1'
                        'scipy>=1.4.1',
                        'shapely>=1.7.0',
                        'skorch>=0.9.0',
                        'torchvision>=0.8.1',
                        'tqdm>=4.44.1',
                        'umap-learn>=0.3.10',
                        'astropy>=4.0.1'],
      zip_safe=False)
