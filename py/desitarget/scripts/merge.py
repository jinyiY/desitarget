import pickle
import numpy as np
import argparse
import glob
import os
from astropy.table import vstack


def merge(flist, path):
    alist = []
    for file in flist:
        with open(file, 'rb') as f:
            alist.append(pickle.load(f))
    #labels = np.concatenate([array[1] for array in alist])
    #labels[labels < 0] = 0
    wave = np.concatenate([array[1] for array in alist])

    ttargets = vstack([tup[2] for tup in alist],join_type='outer')
    ttruth = vstack([tup[3] for tup in alist],join_type='outer')
    tobjtruth = vstack([tup[4] for tup in alist],join_type='outer')
    
    if alist[0][0].shape[1] % 2 == 0:
        a = (np.concatenate([array[0] for array in alist]), wave,ttargets,ttruth,tobjtruth)
    else:
        a = (np.concatenate([array[0][:,:-1] for array in alist]),wave,ttargets,ttruth,tobjtruth)

    with open(path, 'wb') as output:
        pickle.dump(a, output, pickle.HIGHEST_PROTOCOL)
    print('Merging complete.')
    print('Output file: {}'.format(path))
    return


def main():
    # Parse arguments
    args = parser.parse_args()
    flist = args.flist
    path = args.out
    dir = args.dir
    rm = args.rm
    if dir is not None:
        if os.path.isdir(dir):
            print('Directory: ',dir)
            dir_files = glob.glob(os.path.join(dir,'**/*templates*'),recursive=True)
            if type(flist) == list:
                flist.extend(dir_files)
            else:
                flist = dir_files
            print('Files to merge:', *flist,sep='\n')
        else:
            raise NotADirectoryError('{dir} is not a directory')
    if len(flist) == 0:
        raise FileNotFoundError('No files found')
    merge(flist, path)
    if rm:
        for f in flist:
            os.remove(f)



if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--flist', default=None, nargs='*', type=str,
                        help='files to merge')
    parser.add_argument('--dir', default=None, type=str,
                        help='dir to merge')
    parser.add_argument('--out', default=None, type=str,
                        help='path to output')
    parser.add_argument('--rm', default=False, action='store_true',
                        help='remove original files')
    main()
