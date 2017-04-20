# Licensed under a 3-clause BSD style license - see LICENSE.rst
# -*- coding: utf-8 -*-
"""
desitarget.mock.build
=====================

Build a truth catalog (including spectra) and a targets catalog for the mocks.

time python -m cProfile -o mock.dat /usr/local/repos/desihub/desitarget/bin/select_mock_targets -c mock_moustakas.yaml -s 333 --nproc 1 --output_dir proftest
pyprof2calltree -k -i mock.dat &

"""
from __future__ import (absolute_import, division, print_function)

import os
from time import time

import yaml
import numpy as np
from astropy.io import fits
from astropy.table import Table, Column, vstack

from desispec.io.util import fitsheader, write_bintable
from desispec.brick import brickname as get_brickname_from_radec

import desitarget.mock.io as mockio
import desitarget.mock.selection as mockselect
from desitarget.mock.spectra import MockSpectra
from desitarget.internal import sharedmem
from desitarget.targetmask import desi_mask, bgs_mask, mws_mask

from desiutil.log import get_logger, DEBUG
log = get_logger(DEBUG)

def fileid_filename(source_data, output_dir):
    '''
    Outputs text file with mapping between mock filenum and file on disk

    returns mapping dictionary map[mockanme][filenum] = filepath

    '''
    out = open(os.path.join(output_dir, 'map_id_filename.txt'), 'w')
    map_id_name = {}
    for k in source_data.keys():
        map_id_name[k] = {}
        data = source_data[k]
        filenames = data['FILES']
        n_files = len(filenames)
        for i in range(n_files):
            map_id_name[k][i] = filenames[i]
            out.write('{} {} {}\n'.format(k, i, map_id_name[k][i]))
    out.close()

    return map_id_name

class BrickInfo(object):
    """Gather information on all the bricks.

    """
    def __init__(self, random_state=None, dust_dir=None, bounds=(0.0, 360.0, -90.0, 90.0),
                 bricksize=0.25, decals_brick_info=None, target_names=None):
        """Initialize the class.

        Args:
          random_state : random number generator object
          dust_dir : path where the E(B-V) maps are stored
          bounds : brick boundaries
          bricksize : brick size (default 0.25 deg, square)
          decals_brick_info : filename of the DECaLS brick information structure
          target_names : list of targets (e.g., BGS, ELG, etc.)

        """
        if random_state is None:
            random_state = np.random.RandomState()
        self.random_state = random_state

        self.dust_dir = dust_dir
        self.bounds = bounds
        self.bricksize = bricksize
        self.decals_brick_info = decals_brick_info
        self.target_names = target_names

    def generate_brick_info(self):
        """Generate the brick dictionary in the region (min_ra, max_ra, min_dec,
        max_dec).

        [Doesn't this functionality exist elsewhere?!?]
        """
        from desispec.brick import Bricks
        min_ra, max_ra, min_dec, max_dec = self.bounds

        B = Bricks(bricksize=self.bricksize)
        brick_info = {}
        brick_info['BRICKNAME'] = []
        brick_info['RA'] = []
        brick_info['DEC'] =  []
        brick_info['RA1'] =  []
        brick_info['RA2'] =  []
        brick_info['DEC1'] =  []
        brick_info['DEC2'] =   []
        brick_info['BRICKAREA'] =  []

        i_rows = np.where(((B._edges_dec+self.bricksize) >= min_dec) & ((B._edges_dec-self.bricksize) <= max_dec))[0]
        for i_row in i_rows:
            j_col_min = int((min_ra)/360 * B._ncol_per_row[i_row])
            j_col_max = int((max_ra)/360 * B._ncol_per_row[i_row])

            for j_col in range(j_col_min, j_col_max+1):
                brick_info['BRICKNAME'].append(B._brickname[i_row][j_col])

                brick_info['RA'].append(B._center_ra[i_row][j_col])
                brick_info['DEC'].append(B._center_dec[i_row])

                brick_info['RA1'].append(B._edges_ra[i_row][j_col])
                brick_info['DEC1'].append(B._edges_dec[i_row])

                brick_info['RA2'].append(B._edges_ra[i_row][j_col+1])
                brick_info['DEC2'].append(B._edges_dec[i_row+1])

                brick_area = (brick_info['RA2'][-1]- brick_info['RA1'][-1])
                brick_area *= (np.sin(brick_info['DEC2'][-1]*np.pi/180.) -
                               np.sin(brick_info['DEC1'][-1]*np.pi/180.)) * 180 / np.pi
                brick_info['BRICKAREA'].append(brick_area)

        for k in brick_info.keys():
            brick_info[k] = np.array(brick_info[k])

        log.info('Generating brick information for {} brick(s) with boundaries RA={:g}, {:g}, Dec={:g}, {:g} and bricksize {:g} deg.'.\
                 format(len(brick_info['BRICKNAME']), self.bounds[0], self.bounds[1],
                        self.bounds[2], self.bounds[3], self.bricksize))

        return brick_info

    def extinction_across_bricks(self, brick_info):
        """Estimates E(B-V) across bricks.

        Args:
          brick_info : dictionary gathering brick information. It must have at
            least two keys 'RA' and 'DEC'.

        """
        from desitarget.mock import sfdmap

        #log.info('Generated extinction for {} bricks'.format(len(brick_info['RA'])))
        a = {}
        a['EBV'] = sfdmap.ebv(brick_info['RA'], brick_info['DEC'], mapdir=self.dust_dir)

        return a

    def depths_across_bricks(self, brick_info):
        """
        Generates a sample of magnitud dephts for a set of bricks.

        This model was built from the Data Release 3 of DECaLS.

        Args:
            brick_info(Dictionary). Containts at least the following keys:
                RA (float): numpy array of RA positions
                DEC (float): numpy array of Dec positions

        Returns:
            depths (dictionary). keys include
                'DEPTH_G', 'DEPTH_R', 'DEPTH_Z',
                'GALDEPTH_G', 'GALDEPTH_R', 'GALDEPTH_Z'.
                The values ofr each key ar numpy arrays (float) with size equal to
                the input ra, dec arrays.

        """
        ra = brick_info['RA']
        dec = brick_info['DEC']

        n_to_generate = len(ra)
        #mean and std deviation of the difference between DEPTH and GALDEPTH in the DR3 data.
        differences = {}
        differences['DEPTH_G'] = [0.22263251, 0.059752077]
        differences['DEPTH_R'] = [0.26939404, 0.091162138]
        differences['DEPTH_Z'] = [0.34058815, 0.056099825]

        # (points, fractions) provide interpolation to the integrated probability distributions from DR3 data

        points = {}
        points['DEPTH_G'] = np.array([ 12.91721153,  18.95317841,  20.64332008,  23.78604698,  24.29093361,
                      24.4658947,   24.55436325,  24.61874771,  24.73129845,  24.94996071])
        points['DEPTH_R'] = np.array([ 12.91556168,  18.6766777,   20.29519463,  23.41814804,  23.85244179,
                      24.10131454,  24.23338318,  24.34066582,  24.53495026,  24.94865227])
        points['DEPTH_Z'] = np.array([ 13.09378147,  21.06531525,  22.42395782,  22.77471352,  22.96237755,
                      23.04913139,  23.43119431,  23.69817734,  24.1913662,   24.92163849])

        fractions = {}
        fractions['DEPTH_G'] = np.array([0.0, 0.01, 0.02, 0.08, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0])
        fractions['DEPTH_R'] = np.array([0.0, 0.01, 0.02, 0.08, 0.2, 0.3, 0.4, 0.5, 0.7, 1.0])
        fractions['DEPTH_Z'] = np.array([0.0, 0.01, 0.03, 0.08, 0.2, 0.3, 0.7, 0.9, 0.99, 1.0])

        names = ['DEPTH_G', 'DEPTH_R', 'DEPTH_Z']
        depths = {}
        for name in names:
            fracs = self.random_state.random_sample(n_to_generate)
            depths[name] = np.interp(fracs, fractions[name], points[name])

            depth_minus_galdepth = self.random_state.normal(
                loc=differences[name][0],
                scale=differences[name][1], size=n_to_generate)
            depth_minus_galdepth[depth_minus_galdepth<0] = 0.0

            depths['GAL'+name] = depths[name] - depth_minus_galdepth
            #log.info('Generated {} and GAL{} for {} bricks'.format(name, name, len(ra)))

        return depths

    def fluctuations_across_bricks(self, brick_info):
        """
        Generates number density fluctuations.

        Args:
          decals_brick_info (string). file summarizing tile statistics Data Release 3 of DECaLS.
          brick_info(Dictionary). Containts at least the following keys:
            DEPTH_G(float) : array of depth magnitudes in the G band.

        Returns:
          fluctuations (dictionary) with keys 'FLUC+'depth, each one with values
            corresponding to a dictionary with keys ['ALL','LYA','MWS','BGS','QSO','ELG','LRG'].
            i.e. fluctuation[FLUC_DEPTH_G]['MWS'] holds the number density as a funtion
            is a dictionary with keys corresponding to the different galaxy types.

        """
        from desitarget.QA import generate_fluctuations

        fluctuation = {}

        depth_available = []
    #   for k in brick_info.keys():
        for k in ['GALDEPTH_R', 'EBV']:
            if ('DEPTH' in k or 'EBV' in k):
                depth_available.append(k)

        for depth in depth_available:
            fluctuation['FLUC_'+depth] = {}
            for ttype in self.target_names:
                fluctuation['FLUC_'+depth][ttype] = generate_fluctuations(self.decals_brick_info,
                                                                          ttype,
                                                                          depth,
                                                                          brick_info[depth],
                                                                          random_state=self.random_state)
                #log.info('Generated target fluctuation for type {} using {} as input for {} bricks'.format(
                #    ttype, depth, len(fluctuation['FLUC_'+depth][ttype])))

        return fluctuation

    def targetinfo(self):
        """Read target info from DESIMODEL, change all the keys to upper case, and
        append into brick_info.

        """
        filein = open(os.getenv('DESIMODEL')+'/data/targets/targets.dat')
        td = yaml.load(filein)
        target_desimodel = {}
        for t in td.keys():
            if 'ntarget' in t.upper():
                target_desimodel[t.upper()] = td[t]

        return target_desimodel

    def build_brickinfo(self):
        """Build the complete information structure."""

        brick_info = self.generate_brick_info()
        brick_info.update(self.extinction_across_bricks(brick_info))   # add extinction
        brick_info.update(self.depths_across_bricks(brick_info))       # add depths
        brick_info.update(self.fluctuations_across_bricks(brick_info)) # add number density fluctuations
        brick_info.update(self.targetinfo())                           # add nominal target densities

        return brick_info

def add_mock_shapes_and_fluxes(mocktargets, realtargets=None, random_state=None):
    '''Add SHAPEDEV_R and SHAPEEXP_R from a real target catalog.'''

    if random_state is None:
        random_state = np.random.RandomState()

    n = len(mocktargets)

    for objtype in ('ELG', 'LRG', 'QSO'):
        mask = desi_mask.mask(objtype)
        #- indices where mock (ii) and real (jj) match the mask
        ii = np.where((mocktargets['DESI_TARGET'] & mask) != 0)[0]
        jj = np.where((realtargets['DESI_TARGET'] & mask) != 0)[0]
        if len(jj) == 0:
            log.warning('Real target catalog missing {}'.format(objtype))
            raise ValueError

        #- Which random jj should be used to fill in values for ii?
        kk = jj[random_state.randint(0, len(jj), size=len(ii))]
        mocktargets['SHAPEDEV_R'][ii] = realtargets['SHAPEDEV_R'][kk]
        mocktargets['SHAPEEXP_R'][ii] = realtargets['SHAPEEXP_R'][kk]

    for objtype in ('BGS_FAINT', 'BGS_BRIGHT'):
        mask = bgs_mask.mask(objtype)
        #- indices where mock (ii) and real (jj) match the mask
        ii = np.where((mocktargets['BGS_TARGET'] & mask) != 0)[0]
        jj = np.where((realtargets['BGS_TARGET'] & mask) != 0)[0]
        if len(jj) == 0:
            log.warning('Real target catalog missing {}'.format(objtype))
            raise ValueError

        #- Which jj should be used to fill in values for ii?
        #- NOTE: not filling in BGS or MWS fluxes, only shapes
        kk = jj[random_state.randint(0, len(jj), size=len(ii))]
        mocktargets['SHAPEDEV_R'][ii] = realtargets['SHAPEDEV_R'][kk]
        mocktargets['SHAPEEXP_R'][ii] = realtargets['SHAPEEXP_R'][kk]

def empty_targets_table(nobj=1):
    """Initialize an empty 'targets' table.  The required output columns in order
    for fiberassignment to work are: TARGETID, RA, DEC, DESI_TARGET, BGS_TARGET,
    MWS_TARGET, SUBPRIORITY and OBSCONDITIONS.  Everything else is gravy.

    """
    targets = Table()

    # Columns required for fiber assignment:
    targets.add_column(Column(name='TARGETID', length=nobj, dtype='int64'))
    targets.add_column(Column(name='RA', length=nobj, dtype='f8'))
    targets.add_column(Column(name='DEC', length=nobj, dtype='f8'))
    targets.add_column(Column(name='DESI_TARGET', length=nobj, dtype='i8'))
    targets.add_column(Column(name='BGS_TARGET', length=nobj, dtype='i8'))
    targets.add_column(Column(name='MWS_TARGET', length=nobj, dtype='i8'))
    targets.add_column(Column(name='SUBPRIORITY', length=nobj, dtype='f8'))
    targets.add_column(Column(name='OBSCONDITIONS', length=nobj, dtype='i4'))

    # Quantities mimicking a true targeting catalog (or inherited from the
    # mocks).
    targets.add_column(Column(name='BRICKNAME', length=nobj, dtype='U10'))
    targets.add_column(Column(name='DECAM_FLUX', shape=(6,), length=nobj, dtype='f4'))
    targets.add_column(Column(name='WISE_FLUX', shape=(2,), length=nobj, dtype='f4'))
    targets.add_column(Column(name='SHAPEEXP_R', length=nobj, dtype='f4'))
    targets.add_column(Column(name='SHAPEEXP_E1', length=nobj, dtype='f4'))
    targets.add_column(Column(name='SHAPEEXP_E2', length=nobj, dtype='f4'))
    targets.add_column(Column(name='SHAPEDEV_R', length=nobj, dtype='f4'))
    targets.add_column(Column(name='SHAPEDEV_E1', length=nobj, dtype='f4'))
    targets.add_column(Column(name='SHAPEDEV_E2', length=nobj, dtype='f4'))
    targets.add_column(Column(name='DECAM_DEPTH', shape=(6,), length=nobj,
                              data=np.zeros((nobj, 6)), dtype='f4'))
    targets.add_column(Column(name='DECAM_GALDEPTH', shape=(6,), length=nobj,
                              data=np.zeros((nobj, 6)), dtype='f4'))
    targets.add_column(Column(name='EBV', length=nobj, dtype='f4'))

    return targets

def empty_truth_table(nobj=1):
    """Initialize the truth table for each mock object, with spectra.

    """
    truth = Table()
    truth.add_column(Column(name='TARGETID', length=nobj, dtype='int64'))
    truth.add_column(Column(name='MOCKID', length=nobj, dtype='int64'))
    truth.add_column(Column(name='CONTAM_TARGET', length=nobj, dtype='i8'))

    truth.add_column(Column(name='TRUEZ', length=nobj, dtype='f4', data=np.zeros(nobj)))
    truth.add_column(Column(name='TRUESPECTYPE', length=nobj, dtype='U10')) # GALAXY, QSO, STAR, etc.
    truth.add_column(Column(name='TEMPLATETYPE', length=nobj, dtype='U10')) # ELG, BGS, STAR, WD, etc.
    truth.add_column(Column(name='TEMPLATESUBTYPE', length=nobj, dtype='U10')) # DA, DB, etc.

    truth.add_column(Column(name='TEMPLATEID', length=nobj, dtype='i4', data=np.zeros(nobj)-1))
    truth.add_column(Column(name='SEED', length=nobj, dtype='int64', data=np.zeros(nobj)-1))
    truth.add_column(Column(name='MAG', length=nobj, dtype='f4', data=np.zeros(nobj)+99))
    truth.add_column(Column(name='DECAM_FLUX', shape=(6,), length=nobj, dtype='f4'))
    truth.add_column(Column(name='WISE_FLUX', shape=(2,), length=nobj, dtype='f4'))

    truth.add_column(Column(name='OIIFLUX', length=nobj, dtype='f4', data=np.zeros(nobj)-1, unit='erg/(s*cm2)'))
    truth.add_column(Column(name='HBETAFLUX', length=nobj, dtype='f4', data=np.zeros(nobj)-1, unit='erg/(s*cm2)'))

    truth.add_column(Column(name='TEFF', length=nobj, dtype='f4', data=np.zeros(nobj)-1, unit='K'))
    truth.add_column(Column(name='LOGG', length=nobj, dtype='f4', data=np.zeros(nobj)-1, unit='m/(s**2)'))
    truth.add_column(Column(name='FEH', length=nobj, dtype='f4', data=np.zeros(nobj)-1))

    return truth

def _get_spectra_onebrick(specargs):
    """Filler function for the multiprocessing."""
    return get_spectra_onebrick(*specargs)

def get_spectra_onebrick(target_name, mockformat, thisbrick, brick_info, Spectra, source_data, rand):
    """Wrapper function to generate spectra for all the objects on a single brick."""

    brickindx = np.where(brick_info['BRICKNAME'] == thisbrick)[0]
    onbrick = np.where(source_data['BRICKNAME'] == thisbrick)[0]
    nobj = len(onbrick)

    targets = empty_targets_table(nobj)
    truth = empty_truth_table(nobj)

    trueflux, meta = getattr(Spectra, target_name.lower())(source_data, index=onbrick, mockformat=mockformat)

    for key in ('TEMPLATEID', 'SEED', 'MAG', 'DECAM_FLUX', 'WISE_FLUX',
                'OIIFLUX', 'HBETAFLUX', 'TEFF', 'LOGG', 'FEH'):
        truth[key] = meta[key]

    for band, depthkey in zip((1, 2, 4), ('DEPTH_G', 'DEPTH_R', 'DEPTH_Z')):
        targets['DECAM_DEPTH'][:, band] = brick_info[depthkey][brickindx]
    for band, depthkey in zip((1, 2, 4), ('GALDEPTH_G', 'GALDEPTH_R', 'GALDEPTH_Z')):
        targets['DECAM_GALDEPTH'][:, band] = brick_info[depthkey][brickindx]
    targets['EBV'] = brick_info['EBV'][brickindx]

    # Perturb the photometry based on the variance on this brick.  Hack!  Assume
    # a constant depth (22.3-->1.2 nanomaggies, 23.8-->0.3 nanomaggies) in the
    # WISE bands for now.
    wise_onesigma = np.zeros((nobj, 2))
    wise_onesigma[:, 0] = 1.2
    wise_onesigma[:, 1] = 0.3
    targets['WISE_FLUX'] = truth['WISE_FLUX'] + rand.normal(scale=wise_onesigma)

    for band in (1, 2, 4):
        targets['DECAM_FLUX'][:, band] = truth['DECAM_FLUX'][:, band] + \
          rand.normal(scale=1.0/np.sqrt(targets['DECAM_DEPTH'][:, band]))

    return [targets, truth, trueflux, onbrick]

def _write_onebrick(writeargs):
    """Filler function for the multiprocessing."""
    return write_onebrick(*writeargs)

def write_onebrick(thisbrick, targets, truth, trueflux, truthhdr, wave, output_dir):
    """Wrapper function to write out files on a single brick."""

    onbrick = np.where(targets['BRICKNAME'] == thisbrick)[0]

    radir = os.path.join(output_dir, thisbrick[:3])
    targetsfile = os.path.join(radir, 'targets-{}.fits'.format(thisbrick))
    truthfile = os.path.join(radir, 'truth-{}.fits'.format(thisbrick))
    log.info('Writing {}.'.format(truthfile))

    try:
        targets[onbrick].write(targetsfile, overwrite=True)
    except:
        targets[onbrick].write(targetsfile, clobber=True)

    hx = fits.HDUList()
    hdu = fits.ImageHDU(wave.astype(np.float32), name='WAVE', header=truthhdr)
    hx.append(hdu)

    hdu = fits.ImageHDU(trueflux[onbrick, :].astype(np.float32), name='FLUX')
    hdu.header['BUNIT'] = '1e-17 erg/s/cm2/A'
    hx.append(hdu)

    try:
        hx.writeto(truthfile, overwrite=True)
    except:
        hx.writeto(truthfile, clobber=True)

    write_bintable(truthfile, truth[onbrick], extname='TRUTH')

def targets_truth(params, output_dir, realtargets=None, seed=None, verbose=True,
                  bricksize=0.25, outbricksize=0.25, nproc=1):
    """
    Write

    Args:
        params: dict of source definitions.
        output_dir: location for intermediate mtl files.
        realtargets (optional): real target catalog table, e.g. from DR3
        nproc (optional): number of parallel processes to use (default 4)

    Returns:
      targets:
      truth:

    Notes:
      If nproc == 1 use serial instead of parallel code.

    """
    rand = np.random.RandomState(seed)

    # Add the ra,dec boundaries to the parameters dictionary for each source, so
    # we can check the target densities, below.
    if ('subset' in params.keys()) & (params['subset']['ra_dec_cut'] == True):
        bounds = (params['subset']['min_ra'], params['subset']['max_ra'],
                  params['subset']['min_dec'], params['subset']['max_dec'])
    else:
        bounds = (0.0, 360.0, -90.0, 90.0)

    for src in params['sources'].keys():
        params['sources'][src].update({'bounds': bounds})

    # Build the brick information structure.
    brick_info = BrickInfo(random_state=rand, dust_dir=params['dust_dir'], bounds=bounds,
                           bricksize=bricksize, decals_brick_info=params['decals_brick_info'],
                           target_names=list(params['sources'].keys())).build_brickinfo()

    # Initialize the Classes used to assign spectra and select targets.  Note:
    # The default wavelength array gets initialized here, too.
    log.info('Initializing the MockSpectra and SelectTargets classes.')
    Spectra = MockSpectra(rand=rand, verbose=verbose)
    SelectTargets = mockselect.SelectTargets(logger=log, rand=rand,
                                             brick_info=brick_info)
    print()

    # Print info about the mocks we will be loading and then load them.
    if verbose:
        mockio.print_all_mocks_info(params)
        print()
        
    source_data_all = mockio.load_all_mocks(params, rand=rand, bricksize=bricksize, nproc=nproc)
    # map_fileid_filename = fileid_filename(source_data_all, output_dir)
    print()

    # Loop over each source / object type.
    alltargets = list()
    alltruth = list()
    alltrueflux = list()
    for source_name in params['sources'].keys():
        target_name = params['sources'][source_name]['target_name'] # Target type (e.g., ELG)
        mockformat = params['sources'][source_name]['format']

        source_data = source_data_all[source_name]     # data (ra, dec, etc.)

        # If there are no sources, keep going.
        if not bool(source_data):
            continue
        
        nobj = len(source_data['RA'])
        targets = empty_targets_table(nobj)
        truth = empty_truth_table(nobj)
        trueflux = np.zeros((nobj, len(Spectra.wave)), dtype='f4')

        # Assign spectra by parallel-processing the bricks.
        brickname = source_data['BRICKNAME']
        unique_bricks = list(set(brickname))

        # Quickly check that info on all the bricks are here.
        for thisbrick in unique_bricks:
            brickindx = np.where(brick_info['BRICKNAME'] == thisbrick)[0]
            if (len(brickindx) != 1):
                log.fatal('One or too many matching brick(s) {}! This should not happen...'.format(thisbrick))
                raise ValueError
        skyarea = brick_info['BRICKAREA'][0] * len(unique_bricks)
        
        log.info('Assigned {} {}s to {} unique {}x{} deg2 bricks spanning (approximately) {:.4g} deg2.'.format(
            len(brickname), source_name, len(unique_bricks), bricksize, bricksize, skyarea))

        #import matplotlib.pyplot as plt
        #plt.scatter(brick_info['RA1'], brick_info['DEC1'])
        #plt.scatter(brick_info['RA2'], brick_info['DEC2'])
        #plt.scatter(source_data['RA'], source_data['DEC'], alpha=0.1)
        #plt.show()
        #import pdb ; pdb.set_trace()
        
        nbrick = np.zeros((), dtype='i8')
        t0 = time()
        def _update_spectra_status(result):
            if nbrick % 10 == 0 and nbrick > 0:
                rate = (time() - t0) / nbrick
                log.info('{} bricks; {:.1f} sec / brick'.format(nbrick, rate))
            nbrick[...] += 1    # this is an in-place modification
            return result

        specargs = list()
        for thisbrick in unique_bricks:
            specargs.append((target_name, mockformat, thisbrick, brick_info, Spectra, source_data, rand))

        if nproc > 1:
            pool = sharedmem.MapReduce(np=nproc)
            with pool:
                out = pool.map(_get_spectra_onebrick, specargs, reduce=_update_spectra_status)
        else:
            out = list()
            for ii in range(len(unique_bricks)):
                out.append(_update_spectra_status(_get_spectra_onebrick(specargs[ii])))

        for ii in range(len(unique_bricks)):
            targets[out[ii][3]] = out[ii][0]
            truth[out[ii][3]] = out[ii][1]
            trueflux[out[ii][3], :] = out[ii][2]

        targets['RA'] = source_data['RA']
        targets['DEC'] = source_data['DEC']
        targets['BRICKNAME'] = brickname

        if 'SHAPEEXP_R' in source_data.keys(): # not all target types have shape information
            for key in ('SHAPEEXP_R', 'SHAPEEXP_E1', 'SHAPEEXP_E2',
                        'SHAPEDEV_R', 'SHAPEDEV_E1', 'SHAPEDEV_E2'):
                targets[key] = source_data[key]

        truth['MOCKID'] = source_data['MOCKID']
        truth['TRUEZ'] = source_data['Z'].astype('f4')
        truth['TEMPLATETYPE'] = source_data['TEMPLATETYPE']
        truth['TEMPLATESUBTYPE'] = source_data['TEMPLATESUBTYPE']
        truth['TRUESPECTYPE'] = source_data['TRUESPECTYPE']

        # Select targets.
        selection_function = '{}_select'.format(target_name.lower())
        getattr(SelectTargets, selection_function)(targets, truth)

        keep = np.where(targets['DESI_TARGET'] != 0)[0]
        if len(keep) == 0:
            log.warning('No {} targets identified!'.format(target_name))
        else:
            targets = targets[keep]
            truth = truth[keep]
            trueflux = trueflux[keep, :]
            
        # Finally downsample based on the desired number density.
        if 'density' in params['sources'][source_name].keys():
            if verbose:
                print()

            density = params['sources'][source_name]['density']
            if target_name != 'QSO':
                log.info('Downsampling {}s to desired target density of {} targets/deg2.'.format(target_name, density))
                
            if target_name == 'QSO':
                # Distinguish between the Lyman-alpha and tracer QSOs
                if 'LYA' in params['sources'][source_name].keys():
                    density_lya = params['sources'][source_name]['LYA']['density']
                    zcut = params['sources'][source_name]['LYA']['zcut']
                    tracer = np.where(truth['TRUEZ'] < zcut)[0]
                    lya = np.where(truth['TRUEZ'] >= zcut)[0]
                    if len(tracer) > 0:
                        log.info('Downsampling tracer {}s to desired target density of {} targets/deg2.'.format(target_name, density))
                        SelectTargets.density_select(targets[tracer], truth[tracer], source_name=source_name,
                                                     target_name=target_name, density=density)
                        print()
                    if len(lya) > 0:
                        SelectTargets.density_select(targets[lya], truth[lya], source_name=source_name,
                                                     target_name=target_name, density=density_lya)
                        log.info('Downsampling Lya {}s to desired target density of {} targets/deg2.'.format(target_name, density_lya))

                else:
                    SelectTargets.density_select(targets, truth, source_name=source_name,
                                                 target_name=target_name, density=density)
                    
            else:
                SelectTargets.density_select(targets, truth, source_name=source_name,
                                             target_name=target_name, density=density)            

            keep = np.where(targets['DESI_TARGET'] != 0)[0]
            if len(keep) == 0:
                log.warning('All {} targets rejected!'.format(target_name))
            else:
                targets = targets[keep]
                truth = truth[keep]
                trueflux = trueflux[keep, :]

        alltargets.append(targets)
        alltruth.append(truth)
        alltrueflux.append(trueflux)
        print()

    # Consolidate across all the mocks.
    if len(alltargets) == 0:
        log.info('No targets; all done.')
        return

    targets = vstack(alltargets)
    truth = vstack(alltruth)
    trueflux = np.concatenate(alltrueflux)

    # Finally downsample contaminants.  The way this is being done isn't idea
    # because in principle an object could be a contaminant in one target class
    # (and be tossed) but be a contaminant for another target class and be kept.
    # But I think this is mostly OK.
    for source_name in params['sources'].keys():
        target_name = params['sources'][source_name]['target_name'] # Target type (e.g., ELG)
        
        if 'contam' in params['sources'][source_name].keys():
            if verbose:
                print()
            log.info('Downsampling {} contaminant(s) to desired target density.'.format(target_name))
            
            contam = params['sources'][source_name]['contam']
            SelectTargets.contaminants_select(targets, truth, source_name=source_name,
                                              target_name=target_name, contam=contam)
            
            keep = np.where(targets['DESI_TARGET'] != 0)[0]
            if len(keep) == 0:
                log.warning('All {} contaminants rejected!'.format(target_name))
            else:
                targets = targets[keep]
                truth = truth[keep]
                trueflux = trueflux[keep, :]

    # Finally assign TARGETIDs and subpriorities.
    ntarget = len(targets)

    targetid = rand.randint(2**62, size=ntarget)
    truth['TARGETID'] = targetid
    targets['TARGETID'] = targetid
    targets['SUBPRIORITY'] = rand.uniform(0.0, 1.0, size=ntarget)

    if realtargets is not None:
        add_mock_shapes_and_fluxes(targets, realtargets, random_state=rand)

    # Write out the sky catalog.  Should we write "truth.fits" as well?!?
    try:
        os.stat(output_dir)
    except:
        os.makedirs(output_dir)

    skyfile = os.path.join(output_dir, 'sky.fits')
    isky = np.where((targets['DESI_TARGET'] & desi_mask.SKY) != 0)[0]
    nsky = len(isky)
    if nsky:
        log.info('Writing {}'.format(skyfile))
        write_bintable(skyfile, targets[isky], extname='SKY', clobber=True)

        log.info('Removing {} SKY targets from targets, truth, and trueflux.'.format(nsky))
        notsky = np.where((targets['DESI_TARGET'] & desi_mask.SKY) == 0)[0]
        if len(notsky) > 0:
            targets = targets[notsky]
            truth = truth[notsky]
            trueflux = trueflux[notsky, :]
        else:
            log.info('Only SKY targets; returning.')
            return
        print()

    # Write out the dark- and bright-time standard stars.  White dwarf standards
    # not yet supported.
    for suffix, stdbit in zip(('dark', 'bright'), ('STD_FSTAR', 'STD_BRIGHT')):
        stdfile = os.path.join(output_dir, 'standards-{}.fits'.format(suffix))
        #istd = ((targets['DESI_TARGET'] & desi_mask.mask(stdbit)) |
        #        (targets['DESI_TARGET'] & desi_mask.mask('STD_WD'))) != 0
        istd = (targets['DESI_TARGET'] & desi_mask.mask(stdbit)) != 0
        if np.count_nonzero(istd) > 0:
            log.info('Writing {}'.format(stdfile))
            write_bintable(stdfile, targets[istd], extname='STD', clobber=True)
        else:
            log.info('No {} standards found, {} not written.'.format(suffix.upper(), stdfile))

    # Write out the brick-level files (if any).
    targets['BRICKNAME'] = get_brickname_from_radec(targets['RA'], targets['DEC'], bricksize=outbricksize)
    unique_bricks = list(set(targets['BRICKNAME']))
    log.info('Writing out {} targets to {} {}x{} deg2 bricks.'.format(len(targets), len(unique_bricks),
                                                                      outbricksize, outbricksize))
    # Create the RA-slice directories, if necessary and then initialize the output header.
    radir = np.array(['{}'.format(os.path.join(output_dir, name[:3])) for name in targets['BRICKNAME']])
    for thisradir in list(set(radir)):
        try:
            os.stat(thisradir)
        except:
            os.makedirs(thisradir)

    if seed is None:
        seed1 = 'None'
    else:
        seed1 = seed
    truthhdr = fitsheader(dict(
        SEED = (seed1, 'initial random seed'),
        BRICKSZ = (outbricksize, 'brick size (deg)'),
        BUNIT = ('Angstrom', 'wavelength units'),
        AIRORVAC = ('vac', 'vacuum wavelengths')
        ))

    nbrick = np.zeros((), dtype='i8')
    t0 = time()
    def _update_write_status(result):
        if verbose and nbrick % 5 == 0 and nbrick > 0:
            rate = nbrick / (time() - t0)
            print('Writing {} bricks; {:.1f} bricks / sec'.format(nbrick, rate))
        nbrick[...] += 1
        return result

    writeargs = list()
    for thisbrick in unique_bricks:
        writeargs.append((thisbrick, targets, truth, trueflux, truthhdr, Spectra.wave, output_dir))

    if nproc > 1:
        pool = sharedmem.MapReduce(np=nproc)
        with pool:
            pool.map(_write_onebrick, writeargs, reduce=_update_write_status)
    else:
        for ii in range(len(unique_bricks)):
            _update_write_status(_write_onebrick(writeargs[ii]))
