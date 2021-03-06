import argparse
import os
import pickle
import sys
import time

import numpy as np
import pandas as pd

from autocnet.matcher.cuda_extractor import extract_features
from autocnet.utils.utils import tile
from autocnet.io.keypoints import to_hdf

from autocnet_server.camera.csm_camera import create_camera
from autocnet_server.camera import footprint
from autocnet_server.utils.utils import create_output_path
from autocnet_server.config import AutoCNet_Config
from autocnet_server.db.model import Images, Keypoints, Matches, Cameras


from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import create_session, scoped_session, sessionmaker
from geoalchemy2.elements import WKTElement

import requests
import json

from plio.io.io_gdal import GeoDataset
import pyproj
import ogr

import Pyro4

import autocnet
funcs = {'vlfeat':autocnet.matcher.cpu_extractor.extract_features}

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("input_file")
    parser.add_argument("callback_uri", help="The callback URI in the form pyro:<obj_name>@<hostname>:<port>")
    parser.add_argument('-t', '--threshold', help='The threshold difference between DN values')
    parser.add_argument('-n', '--nfeatures', help='The number of features to extract. Default is max_image_dimension / 1.25', type=float)
    parser.add_argument('-m', '--maxsize',type=float, default=6e7, help='The maximum number of pixels before tiling is used to extract keypoints.  Default: 6e7')
    parser.add_argument('-e', '--extractor', default='vlfeat', choices=['cuda', 'vlfeat'], help='The extractor to use to get keypoints.')
    parser.add_argument('-c', '--camera', action='store_false', help='Whether or not to compute keypoints coordinates in body fixed as well as image space.')
    parser.add_argument('-o', '--outdir', type=str, help='The output directory')
    return vars(parser.parse_args())

def extract(ds, extractor, maxsize):
    #TODO: Move this into a testable place
    if ds.raster_size[0] * ds.raster_size[1] > maxsize:
        slices = tile(ds.raster_size, tilesize=12000, overlap=250)
    else:
        slices = [[0,0,ds.raster_size[0], ds.raster_size[1]]]

    extractor_params = {'compute_descriptor': True,
                        'float_descriptors': True,
                        'edge_thresh':2.5,
                        'peak_thresh': 0.0001,
                        'verbose': False}
    keypoints = pd.DataFrame()
    descriptors = None
    for s in slices:
        xystart = [s[0], s[1]]
        array = ds.read_array(pixels=s)

        kps, desc = funcs[extractor](array, extractor_method='vlfeat', extractor_parameters=extractor_params)

        kps['x'] += xystart[0]
        kps['y'] += xystart[1]

        count = len(keypoints)
        keypoints = pd.concat((keypoints, kps))
        descriptor_mask = keypoints.duplicated()

        # Removed duplicated and re-index the merged keypoints
        keypoints.drop_duplicates(inplace=True)
        keypoints.reset_index(inplace=True, drop=True)

        if descriptors is not None:
            descriptors = np.concatenate((descriptors, desc))
        else:
            descriptors = desc
        descriptors = descriptors[~descriptor_mask]
        #self.descriptors = descriptors

    return keypoints, descriptors

def finalize(data, callback_uri):
    for k,v in data.items():
        if isinstance(v, np.ndarray):
            data[k] = v.tolist()
    uri = Pyro4.URI(callback_uri)
    with Pyro4.Proxy(uri) as obj:
        obj.add_image_callback(data)

if __name__ == '__main__':
    # Setup the metadata obj that will be written to the db
    metadata = {}

    # Parse args and grab the file handle to the image
    kwargs = parse_args()
    input_file = kwargs.pop('input_file', None)

    #TODO: Tons of logic in here to get extracted
    #try:

    config = AutoCNet_Config()
    db_uri = 'postgresql://{}:{}@{}:{}/{}'.format(config.database_username,
                                                  config.database_password,
                                                  config.database_host,
                                                  config.database_port,
                                                  config.database_name)

    ds = GeoDataset(input_file)

    # Create a camera model for the image
    camera = kwargs.pop('camera')
    camera = create_camera(ds)

    #try:
    # Extract the correspondences
    extractor = kwargs.pop('extractor')
    maxsize = kwargs.pop('maxsize')
    keypoints, descriptors = extract(ds, extractor, maxsize)

    # Setup defaults for the footprints
    footprint_latlon = None
    footprint_bodyfixed = None

    # Project the sift keypoints to the ground
    def func(row, args):
        camera = args[0]
        gnd = getattr(camera, 'imageToGround')(row[1], row[0], 0)
        return gnd

    feats = keypoints[['x', 'y']].values
    gnd = np.apply_along_axis(func, 1, feats, args=(camera, ))

    gnd = pd.DataFrame(gnd, columns=['xm', 'ym', 'zm'], index=keypoints.index)
    keypoints = pd.concat([keypoints, gnd], axis=1)

    footprint_latlon = footprint.generate_latlon_footprint(camera)
    footprint_latlon = footprint_latlon.ExportToWkt()
    if footprint_latlon:
        footprint_latlon = WKTElement(footprint_latlon, srid=config.srid)

    footprint_bodyfixed = footprint.generate_bodyfixed_footprint(camera)
    footprint_bodyfixed = footprint_bodyfixed.ExportToWkt()
    if footprint_bodyfixed:
        footprint_bodyfixed = WKTElement(footprint_bodyfixed)

    # Write the correspondences to disk
    outdir = kwargs.pop('outdir')
    outpath = create_output_path(ds, outdir)
    to_hdf(keypoints, descriptors, outpath)

    # Default response
    data = {'success':False,'path':input_file}

    # Connect to the DB and write
    maxretries = 3

    # Create the DB objs
    camera = pickle.dumps(camera, 2)
    c = Cameras(camera=camera)
    k = Keypoints(path=outpath, nkeypoints=len(keypoints))
    img = Images(name=ds.file_name, path=input_file,
                 footprint_latlon=footprint_latlon,
                 cameras=c,keypoints=k)

    # Attempt to grab an available db connection
    session = None
    while maxretries:
        try:
            engine = create_engine(db_uri, poolclass=NullPool)
            connection = engine.connect()
            session = sessionmaker(bind=engine, autoflush=True)()
            break
        except:
            time.sleep(30)
            maxretries -= 1
    if session:
            session.add(img)
            session.commit()
            session.close()
            # Send the success signal to the listener
            data = {'success':True,'path':input_file}
    finalize(data, kwargs['callback_uri'])
