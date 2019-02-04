import argparse
import desisim.templates as tmpl
import os
import numpy as np
import pickle
from desiutil.log import get_logger, DEBUG
from desitarget.mock.mockmaker import BGSMaker, ELGMaker,LRGMaker




parser = argparse.ArgumentParser()
parser.add_argument('--nall', default=0, type=int,
                help='integer: number of spectra to simulate from each type')
parser.add_argument('--nbgs', default=0, type=int,
                help='integer: number of BGS spectra to simulate')
parser.add_argument('--nelg', default=0, type=int,
                help='integer: number of ELG spectra to simulate')
parser.add_argument('--nlrg', default=0, type=int,
                help='integer: number of LRG spectra to simulate')

parser.add_argument('--add_sn_ia', action='store_true', default=False,
                help='boolean: inject supernova Ia')
parser.add_argument('--add_sn_iip',  action='store_true',default=False,
                help='boolean: inject supernova II P')

parser.add_argument('--seed', default=None, type=int,
                help='seed')
parser.add_argument('--nside', default=64, type=int,
                help='nside for healpixel')
parser.add_argument('--healpixel', default=26030, type=int,
                help='healpixel number')

parser.add_argument('--path', default='./simulation_out', type=str,
                help='path to output')
parser.add_argument('--fname', default='spectra.bin', type=str,
                help='File name')
parser.add_argument('--flux_ratio_range', nargs='*', default=(0.01, 1.0), type=float,
                help='SN flux ratio in r-band')


def _default_wave(wavemin=None, wavemax=None, dw=0.2):
    """Generate a default wavelength vector for the output spectra."""
    from desimodel.io import load_throughput
    
    if wavemin is None:
        wavemin = load_throughput('b').wavemin - 10.0
    if wavemax is None:
        wavemax = load_throughput('z').wavemax + 10.0
            
    return np.arange(round(wavemin, 1), wavemax, dw)

def save(obj, filename):
    filename = os.path.abspath(filename)
    base = os.path.abspath(filename)
    i = 0
    while os.path.isfile(filename):
        i += 1
        filename = ''.join(base.split('.')[:-1]) + f'({str(i)}).' + base.split('.')[-1]
    with open(filename, 'wb') as output:  # Overwrites any existing file.
        pickle.dump(obj, output, pickle.HIGHEST_PROTOCOL)
    return filename

def mockmaker(Maker,template_maker, seed=None, nrand=16,sne_fluxratiorange=(0.01,1),sne_filter='decam2014-r',healpixel = 26030, nside = 64):
    log = get_logger()   
    rand = np.random.RandomState(seed)
    TARGET = Maker(seed=seed)
    # This is the important part
    # Passing a BGS template instance with add_SNeIa 
    TARGET.template_maker = template_maker
    log.info('Reading the mock catalog for {}s'.format(TARGET.objtype))
    tdata = TARGET.read(healpixels=healpixel, nside=nside)
    tdata['sne_fluxratiorange'] = sne_fluxratiorange
    tdata['sne_filter'] = sne_filter 
    
    log.info('Generating {} random spectra.'.format(nrand))
    indx = rand.choice(len(tdata['RA']), np.min( (nrand, len(tdata['RA'])) ) )
    tflux, twave, ttargets, ttruth, tobjtruth = TARGET.make_spectra(tdata, indx=indx)
    
    log.info('Selecting targets')
    TARGET.select_targets(ttargets, ttruth)
    return tflux, twave, ttargets, ttruth, tobjtruth

def main():
    # Parse arguments
    args = parser.parse_args()
    nmodel = args.nall
    path = args.outpath
    fname = args.fname
    add_SNeIa = args.add_sn_ia
    add_SNeIIp = args.add_sn_iip
    flux_ratio = args.flux_ratio_range
    healpixel = args.healpixel
    nside = args.nside
    seed = args.seed
    rand = np.random.RandomState(seed)
    if not os.path.isdir(path):
        os.makedirs(path)
    if not os.access(path, os.W_OK):
        raise PermissionError('You don\'t have persmission to write: {}'.format(path))


    if len(flux_ratio) == 1:
        flux_ratio_range = [flux_ratio[0], flux_ratio[0] + 0.001]
        print('here')
    elif len(flux_ratio) == 2:
        flux_ratio_range = flux_ratio
    else:
        raise ValueError('flux_ratio_range must be one or two floats')


    if nmodel != 0:
        nbgs, nelg, nlrg = [int(nmodel)] * 3
    else:
        nbgs = int(args.nbgs)
        nelg = int(args.nelg)
        nlrg = int(args.nlrg)

    templates_bgs,templates_elg,templates_lrg = [],[],[]

    if nbgs != 0:
        nrand = nbgs        
        Maker = BGSMaker
        template_maker = tmpl.BGS(add_SNeIa=add_SNeIa,wave=_default_wave())
        print('making {} BGS spectra with SN Ia {} SN IIp {}'.format(nbgs, add_SNeIa, add_SNeIIp))
        tflux, twave, ttargets, ttruth, tobjtruth = mockmaker(Maker,template_maker, seed=seed, nrand=nrand,sne_fluxratiorange=flux_ratio_range
            ,sne_filter='decam2014-r',healpixel=healpixel,nside=nside)
        templates_bgs = [tflux, twave, ttargets, ttruth, tobjtruth]
        saved_to = save(templates_bgs,os.path.join(path, fname))
        print("Saved BGS template data to {}".format(saved_to))
    if nelg != 0:
        nrand = nelg     
        Maker = ELGMaker
        template_maker = tmpl.ELG(add_SNeIa=add_SNeIa,wave=_default_wave())
        print('making {} ELG spectra with SN Ia {} SN IIp {}'.format(nelg, add_SNeIa, add_SNeIIp))
        tflux, twave, ttargets, ttruth, tobjtruth = mockmaker(Maker,template_maker, seed=seed, nrand=nrand,sne_fluxratiorange=flux_ratio_range
            ,sne_filter='decam2014-r',healpixel=healpixel,nside=nside)
        templates_bgs = [tflux, twave, ttargets, ttruth, tobjtruth]
        saved_to = save(templates_bgs,os.path.join(path,fname))
        print("Saved ELG template data to {}".format(saved_to))
    if nlrg != 0:
        nrand = nlrg       
        Maker = LRGMaker
        template_maker = tmpl.LRG(add_SNeIa=add_SNeIa,wave=_default_wave())
        print('making {} LRG spectra with SN Ia {} SN IIp {}'.format(nlrg, add_SNeIa, add_SNeIIp))
        tflux, twave, ttargets, ttruth, tobjtruth = mockmaker(Maker,template_maker, seed=seed, nrand=nrand,sne_fluxratiorange=flux_ratio_range
            ,sne_filter='decam2014-r',healpixel=healpixel,nside=nside)
        templates_bgs = [tflux, twave, ttargets, ttruth, tobjtruth]
        saved_to = save(templates_bgs,os.path.join(path,fname))
        print("Saved LRG template data to {}".format(saved_to))
    # data = np.concatenate([t[0] for t in
    #                      [templates_bgs, templates_elg, templates_lrg] if len(t) != 0])

    # labels = np.concatenate([np.ones(nbgs),np.ones(nelg)*2,np.ones(nlrg)*3])
    # c =1
    # if add_SNeIa:
    #     c = 0
    # labeled_data = (data,np.ones(data.shape[0])*c)
    # saved_to = save(labeled_data, os.path.join(path, 'labeled_data.bin'))
    # print("Saved all labeled data to {}".format(saved_to))

if __name__ == '__main__':
    main()
