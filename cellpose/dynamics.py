from scipy.ndimage.filters import maximum_filter1d
import scipy.ndimage
import skimage.morphology
import numpy as np
import numpy.ma as ma
import skfmm
# from tqdm import trange
from tqdm.auto import trange
import time
import mxnet as mx
import mxnet.ndarray as nd
from numba import njit, float32, int32, vectorize
from . import utils, metrics

@njit('(float64[:], int32[:], int32[:], int32, int32, int32, int32)')
def _extend_centers(T,y,x,ymed,xmed,Lx, niter):
    """ run diffusion from center of mask (ymed, xmed) on mask pixels (y, x)

    Parameters
    --------------

    T: float64, array
        _ x Lx array that diffusion is run in

    y: int32, array
        pixels in y inside mask

    x: int32, array
        pixels in x inside mask

    ymed: int32
        center of mask in y

    xmed: int32
        center of mask in x

    Lx: int32
        size of x-dimension of masks

    niter: int32
        number of iterations to run diffusion

    Returns
    ---------------

    T: float64, array
        amount of diffused particles at each pixel

    """
    ## The function here is somehow similar to distance tranform where 
    ## center pixel will have highest value while border pixels go down to zero
    ## The T[y*Lx + x] is original pixel array (in 1D),T[(y-1)*Lx + x] is shifted 1pixel in y
    ## T[y*Lx + x+1] is shifted 1pixel in x. So adding them up is the same as looping through each pixel 
    ## then add up the 8 pixels surrending it (plus itself). But the calculation is done in one go as 
    ## y and x are arrays
    for t in range(niter):
        T[ymed*Lx + xmed] += 1
        T[y*Lx + x] = 1/9. * (T[y*Lx + x] + T[(y-1)*Lx + x]   + T[(y+1)*Lx + x] +
                                            T[y*Lx + x-1]     + T[y*Lx + x+1] +
                                            T[(y-1)*Lx + x-1] + T[(y-1)*Lx + x+1] +
                                            T[(y+1)*Lx + x-1] + T[(y+1)*Lx + x+1])
    return T

def labels_to_flows(labels):
    """ convert labels (list of masks or flows) to flows for training model 

    Parameters
    --------------

    labels: list of ND-arrays
        labels[k] can be 2D or 3D, if [3 x Ly x Lx] then it is assumed that flows were precomputed.
        Otherwise labels[k][0] or labels[k] (if 2D) is used to create flows and cell probabilities.

    Returns
    --------------

    flows: list of [3 x Ly x Lx] arrays
        flows[k][0] is cell probability, flows[k][1] is Y flow, and flows[k][2] is X flow

    """

    nimg = len(labels)
    if labels[0].ndim < 3:
        labels = [labels[n][np.newaxis,:,:] for n in range(nimg)]

    if labels[0].shape[0] == 1 or labels[0].ndim < 3:
        print('NOTE: computing flows for labels (could be done before to save time)')
        # compute flows        
        veci = [masks_to_flows(labels[n][0])[0] for n in trange(nimg)]
        # concatenate flows with cell probability
        flows = [np.concatenate((labels[n][[0]]>0.5, veci[n]), axis=0).astype(np.float32)
                    for n in range(nimg)]
    else:
        print('flows precomputed')
        if labels[0].shape[0] > 3:
            flows = [labels[n][1:].astype(np.float32) for n in range(nimg)]
        else:
            flows = [labels[n].astype(np.float32) for n in range(nimg)]
    return flows

def masks_to_flows(masks):
    """ convert masks to flows using diffusion from center pixel

    Center of masks where diffusion starts is defined to be the 
    closest pixel to the median of all pixels that is inside the 
    mask. Result of diffusion is converted into flows by computing
    the gradients of the diffusion density map. 

    Parameters
    -------------

    masks: int, 2D or 3D array
        labelled masks 0=NO masks; 1,2,...=mask labels

    Returns
    -------------

    mu: float, 3D or 4D array 
        flows in Y = mu[-2], flows in X = mu[-1].
        if masks are 3D, flows in Z = mu[0].

    mu_c: float, 2D or 3D array
        for each pixel, the distance to the center of the mask 
        in which it resides 

    """
    if masks.ndim > 2:
        Lz, Ly, Lx = masks.shape
        mu = np.zeros((3, Lz, Ly, Lx), np.float32)
        for z in range(Lz):
            mu0,_ = masks_to_flows(masks[z])
            mu[[1,2], z] += mu0
        for y in range(Ly):
            mu0,_ = masks_to_flows(masks[:,y])
            mu[[0,2], :, y] += mu0
        for x in range(Lx):
            mu0,_ = masks_to_flows(masks[:,:,x])
            mu[[0,1], :, :, x] += mu0
        return mu, None

    Ly, Lx = masks.shape
    mu = np.zeros((2, Ly, Lx), np.float64)
    mu_c = np.zeros((Ly, Lx), np.float64)
    
    nmask = masks.max()
    slices = scipy.ndimage.find_objects(masks)
    dia = utils.diameters(masks)[0]
    ## 0.15 is the factor of cell center to mask area (only inner most 15% are counted in mu_c). 
    ## If 1.0, then the whole mask is preserved with center value =1 and goes down to 0 toward periphery
    ## but the mu_c is not used at all ?!
    s2 = (0.15 * dia)**2
    for i,si in enumerate(slices):
        if si is not None:
            sr,sc = si
            ly, lx = sr.stop - sr.start + 1, sc.stop - sc.start + 1
            y,x = np.nonzero(masks[sr, sc] == (i+1))
            y = y.astype(np.int32) + 1
            x = x.astype(np.int32) + 1
            ymed = np.median(y)
            xmed = np.median(x)
            imin = np.argmin((x-xmed)**2 + (y-ymed)**2)
            xmed = x[imin]
            ymed = y[imin]

            d2 = (x-xmed)**2 + (y-ymed)**2
            mu_c[sr.start+y-1, sc.start+x-1] = np.exp(-d2/s2)

            niter = 2*np.int32(np.ptp(x) + np.ptp(y))
            T = np.zeros((ly+2)*(lx+2), np.float64)
            T = _extend_centers(T, y, x, ymed, xmed, np.int32(lx), niter)
            T[(y+1)*lx + x+1] = np.log(1.+T[(y+1)*lx + x+1])

            dy = T[(y+1)*lx + x] - T[(y-1)*lx + x]
            dx = T[y*lx + x+1] - T[y*lx + x-1]
            mu[:, sr.start+y-1, sc.start+x-1] = np.stack((dy,dx))

    ## normalized by sqrt(dx^2+dy^2), obtain relative value regarding to each mask(heat source)?
    mu /= (1e-20 + (mu**2).sum(axis=0)**0.5)

    return mu, mu_c


def labels_to_flows2(labels):
    """ Use GDT method to convert labels (list of masks or flows) 
        to flows for training model.

    Parameters
    --------------

    labels: list of ND-arrays
        labels[k] can be 2D or 3D, if [3 x Ly x Lx] then it is assumed that flows were precomputed.
        Otherwise labels[k][0] or labels[k] (if 2D) is used to create flows and cell probabilities.

    Returns
    --------------

    flows: list of [3 x Ly x Lx] arrays
        flows[k][0] is cell probability, flows[k][1] is Y flow, and flows[k][2] is X flow

    """

    nimg = len(labels)
#     if labels[0].ndim < 3:
#         labels = [labels[n][np.newaxis,:,:] for n in range(nimg)]

    if labels[0].shape[0] == 1 or labels[0].ndim < 3:
        print('NOTE: computing flows for labels (could be done before to save time)')
        # compute flows and median diameter
        veci, diam = zip(*[masks_to_flows2(labels[n]) for n in trange(nimg)])
        # concatenate flows with cell probability
        flows = [np.concatenate((np.expand_dims(labels[n], axis=0)>0, veci[n]), axis=0).astype(np.float32)
                    for n in range(nimg)]
    else:
        print('flows precomputed')
        if labels[0].shape[0] > 3:
            flows = [labels[n][1:].astype(np.float32) for n in range(nimg)]
        else:
            flows = [labels[n].astype(np.float32) for n in range(nimg)]
    return flows, diam


def masks_to_flows2(masks):
    """ 
    use the geodesics distance transform and np.gradient to create flow map
    3D is not yet tested thus not implemented. 
    mu_c in the original function not used
    """
    if masks.ndim > 2:
        raise ValueError('3D input not yet implemented')

    Ly, Lx = masks.shape
    mu = np.zeros((2, Ly, Lx), np.float64)
    dia = utils.diameters(masks)[0]
    slices = scipy.ndimage.find_objects(masks)
    for i,si in enumerate(slices):
        if si is not None:
            sr,sc = si
            submask = masks[si].copy()
            m = submask!=(i+1)
            y,x = np.nonzero(~m)
            ## drop 1 pixel width label
            if len(np.unique(x))<2 or len(np.unique(y))<2:
                continue
            centroid = scipy.ndimage.measurements.center_of_mass(submask, labels=submask, index=(i+1))
            ## make sure the centroid is inside the mask otherwise take the point closest to the centroid
            idx_c = np.argmin((y-centroid[0])**2 + (x-centroid[1])**2)
            c = (y[idx_c], x[idx_c])
            ## set the centroid to 0 as the for GDT
            submask[c]=0 
            m_submask = ma.masked_array(submask, m)
            ## do GDT with regard to the mask center (0)
            gdt = skfmm.distance(m_submask)
            ## Calculate the derivatives in y,x 
            g_dy, g_dx = np.gradient(gdt, 1, edge_order=1)
            ## do a 3x3 mean filter to smooth out the border values (to avoid zero gradient at some pixels)
            smoothed_y = ma.masked_array(scipy.ndimage.uniform_filter(ma.filled(g_dy,0), size=3),m)
            smoothed_x = ma.masked_array(scipy.ndimage.uniform_filter(ma.filled(g_dx,0), size=3),m)
            mu[:, sr.start + y, sc.start + x] = np.stack((smoothed_y.compressed(),smoothed_x.compressed()))
    
    ## the GDT method give flow in the opposite direction as the original method. multiply by -1 to make it compatible
    return -1. * mu, dia


@njit('(float32[:,:,:,:],float32[:,:,:,:], int32[:,:], int32)')
def steps3D(p, dP, inds, niter):
    """ run dynamics of pixels to recover masks in 3D
    
    Euler integration of dynamics dP for niter steps

    Parameters
    ----------------

    p: float32, 4D array
        pixel locations [axis x Lz x Ly x Lx] (start at initial meshgrid)

    dP: float32, 4D array
        flows [axis x Lz x Ly x Lx]

    inds: int32, 2D array
        non-zero pixels to run dynamics on [npixels x 3]

    niter: int32
        number of iterations of dynamics to run

    Returns
    ---------------

    p: float32, 4D array
        final locations of each pixel after dynamics

    """
    shape = p.shape[1:]
    for t in range(niter):
        #pi = p.astype(np.int32)
        for j in range(inds.shape[0]):
            z = inds[j,0]
            y = inds[j,1]
            x = inds[j,2]
            p0, p1, p2 = int(p[0,z,y,x]), int(p[1,z,y,x]), int(p[2,z,y,x])
            p[0,z,y,x] = min(shape[0]-1, max(0, p[0,z,y,x] - dP[0,p0,p1,p2]))
            p[1,z,y,x] = min(shape[1]-1, max(0, p[1,z,y,x] - dP[1,p0,p1,p2]))
            p[2,z,y,x] = min(shape[2]-1, max(0, p[2,z,y,x] - dP[2,p0,p1,p2]))
    return p

@njit('(float32[:,:,:], float32[:,:,:], int32[:,:], int32)')
def steps2D(p, dP, inds, niter):
    """ run dynamics of pixels to recover masks in 2D
    
    Euler integration of dynamics dP for niter steps

    Parameters
    ----------------

    p: float32, 3D array
        pixel locations [axis x Ly x Lx] (start at initial meshgrid)

    dP: float32, 3D array
        flows [axis x Ly x Lx]

    inds: int32, 2D array
        non-zero pixels to run dynamics on [npixels x 2]

    niter: int32
        number of iterations of dynamics to run

    Returns
    ---------------

    p: float32, 3D array
        final locations of each pixel after dynamics

    """
    shape = p.shape[1:]
    for t in range(niter):
        #pi = p.astype(np.int32)
        for j in range(inds.shape[0]):
            y = inds[j,0]
            x = inds[j,1]
            p0, p1 = int(p[0,y,x]), int(p[1,y,x])
            p[0,y,x] = min(shape[0]-1, max(0, p[0,y,x] - dP[0,p0,p1]))
            p[1,y,x] = min(shape[1]-1, max(0, p[1,y,x] - dP[1,p0,p1]))
    return p

def follow_flows(dP, niter=200):
    """ define pixels and run dynamics to recover masks in 2D
    
    Pixels are meshgrid. Only pixels with non-zero cell-probability
    are used (as defined by inds)

    Parameters
    ----------------

    dP: float32, 3D or 4D array
        flows [axis x Ly x Lx] or [axis x Lz x Ly x Lx]

    niter: int (optional, default 200)
        number of iterations of dynamics to run

    Returns
    ---------------

    p: float32, 3D array
        final locations of each pixel after dynamics

    """
    shape = np.array(dP.shape[1:]).astype(np.int32)
    niter = np.int32(niter)
    if len(shape)>2:
        p = np.meshgrid(np.arange(shape[0]), np.arange(shape[1]),
                np.arange(shape[2]), indexing='ij')
        p = np.array(p).astype(np.float32)
        # run dynamics on subset of pixels
        inds = np.array(np.nonzero(np.abs(dP[0])>1e-3)).astype(np.int32).T
        p = steps3D(p, dP, inds, niter)
    else:
        p = np.meshgrid(np.arange(shape[0]), np.arange(shape[1]), indexing='ij')
        p = np.array(p).astype(np.float32)
        # run dynamics on subset of pixels
#         inds = np.array(np.nonzero(np.abs(dP[0])>1e-3)).astype(np.int32).T
        inds = np.array(np.nonzero((np.abs(dP[0])>0) | (np.abs(dP[1])>0) )).astype(np.int32).T
        p = steps2D(p, dP, inds, niter)
    return p

def remove_bad_flow_masks(masks, flows, threshold=0.4):
    """ remove masks which have inconsistent flows 
    
    Uses metrics.flow_error to compute flows from predicted masks 
    and compare flows to predicted flows from network. Discards 
    masks with flow errors greater than the threshold.

    Parameters
    ----------------

    masks: int, 2D or 3D array
        labelled masks, 0=NO masks; 1,2,...=mask labels,
        size [Ly x Lx] or [Lz x Ly x Lx]

    flows: float, 3D or 4D array
        flows [axis x Ly x Lx] or [axis x Lz x Ly x Lx]

    threshold: float (optional, default 0.4)
        masks with flow error greater than threshold are discarded.

    Returns
    ---------------

    masks: int, 2D or 3D array
        masks with inconsistent flow masks removed, 
        0=NO masks; 1,2,...=mask labels,
        size [Ly x Lx] or [Lz x Ly x Lx]
    
    """
    merrors, _ = metrics.flow_error(masks, flows)
    badi = 1+(merrors>threshold).nonzero()[0]
    masks[np.isin(masks, badi)] = 0
    return masks

def get_masks(p, iscell=None, rpad=20, flows=None, threshold=0.4):
    """ create masks using pixel convergence after running dynamics
    
    Makes a histogram of final pixel locations p, initializes masks 
    at peaks of histogram and extends the masks from the peaks so that
    they include all pixels with more than 2 final pixels p. Discards 
    masks with flow errors greater than the threshold. 

    Parameters
    ----------------

    p: float32, 3D or 4D array
        final locations of each pixel after dynamics,
        size [axis x Ly x Lx] or [axis x Lz x Ly x Lx].

    iscell: bool, 2D or 3D array
        if iscell is not None, set pixels that are 
        iscell False to stay in their original location.

    rpad: int (optional, default 20)
        histogram edge padding

    threshold: float (optional, default 0.4)
        masks with flow error greater than threshold are discarded 
        (if flows is not None)

    flows: float, 3D or 4D array (optional, default None)
        flows [axis x Ly x Lx] or [axis x Lz x Ly x Lx]. If flows
        is not None, then masks with inconsistent flows are removed using 
        `remove_bad_flow_masks`.

    Returns
    ---------------

    M0: int, 2D or 3D array
        masks with inconsistent flow masks removed, 
        0=NO masks; 1,2,...=mask labels,
        size [Ly x Lx] or [Lz x Ly x Lx]
    
    """
    
    pflows = []
    edges = []
    shape0 = p.shape[1:]
    dims = len(p)
    if iscell is not None:
        if dims==3:
            inds = np.meshgrid(np.arange(shape0[0]), np.arange(shape0[1]),
                np.arange(shape0[2]), indexing='ij')
        elif dims==2:
            inds = np.meshgrid(np.arange(shape0[0]), np.arange(shape0[1]),
                     indexing='ij')
        for i in range(dims):
            p[i, ~iscell] = inds[i][~iscell]

    for i in range(dims):
        pflows.append(p[i].flatten().astype('int32'))
        edges.append(np.arange(-.5-rpad, shape0[i]+.5+rpad, 1))

    ## h has dimension p.x + 2*pad, p.y + 2*pad
    h,_ = np.histogramdd(tuple(pflows), bins=edges)
    hmax = h.copy()
    for i in range(dims):
        hmax = maximum_filter1d(hmax, 5, axis=i)

    seeds = np.nonzero(np.logical_and(h-hmax>-1e-6, h>10))
    Nmax = h[seeds]
    isort = np.argsort(Nmax)[::-1]
    for s in seeds:
        s = s[isort]

    pix = list(np.array(seeds).T)

    shape = h.shape
    if dims==3:
        expand = np.nonzero(np.ones((3,3,3)))
    else:
        expand = np.nonzero(np.ones((3,3)))
    for e in expand:
        e = np.expand_dims(e,1)

    ## why do 5 iteration?
    ## pix = list of the original peak pixel coordiates at h (seed array)
    for iter in range(5):
        ## loop through each peak pixel 
        for k in range(len(pix)):
            ## turn pix[k] array of (x,y) into list for pix[k][i] element assignment 
            if iter==0:
                pix[k] = list(pix[k])
            newpix = []
            iin = []
            for i,e in enumerate(expand):
                ## epix is the indics of y-1, y, y+1; x-1, x, x+1; where x,y is the peak/center identified in h (seed)
                epix = e[:,np.newaxis] + np.expand_dims(pix[k][i], 0) - 1
                epix = epix.flatten()
                ## check whether coordinates are within the image bondaries
                iin.append(np.logical_and(epix>=0, epix<shape[i]))
                newpix.append(epix)
            ## check in all axis (x,y,z) whether any out of boundary coordinates exist
            iin = np.all(tuple(iin), axis=0)
            ## remove out of boundary coordinates from newpix list of arrays [array(y-1, y, y+1), array(x-1, x, x+1)]
            ## but newpix won't be modified inplace within the loop....
            ## should use enumerate(newpix) and newpix[i] = p[iin] instead
            for p in newpix:
                p = p[iin]
            ## probably not required to turn a list into tuple (if just for indexing)
            newpix = tuple(newpix)
            ## why do the peak count filtering again ? as already done in the seed creation step
            igood = h[newpix]>2
            for i in range(dims):
                ## only possible after turn pix[k] array into list
                pix[k][i] = newpix[i][igood]
            if iter==4:
                ## change pix from list into tuple at the end of iteration
                pix[k] = tuple(pix[k])
    
    M = np.zeros(h.shape, np.int32)
    ## fill M at the filtered peak(center)pixel with label number (from 1 to nmask)
    for k in range(len(pix)):
        M[pix[k]] = 1+k
        
    ## pad the pflows 1D array to be aligned with the dimension of h/M (padded with rpad)
    for i in range(dims):
        pflows[i] = pflows[i] + rpad
    
    ## M0 after indexing with padded pflows turns pflows(p in 1D) pixel values into the mask label is belongs to
    ## which originally indicate the mask center coordinates.
    ## M0 dimension is the cropped M (with unpadded dimension as pflows (padded))
    M0 = M[tuple(pflows)]
    _,counts = np.unique(M0, return_counts=True)
    
    # remove big masks
    big = shape0[0] * shape0[1] * 0.35
    for i in np.nonzero(counts > big)[0]:
        M0[M0==i] = 0
    
    ## renumber the labels by np.unique after removing the big masks
    _,M0 = np.unique(M0, return_inverse=True)
    M0 = np.reshape(M0, shape0)

    ## compare the dP calculated from the predicted mask with the dP directly from the model
    ## since the predicted mask is also calculated from the dP of model output. It is basically the
    ## error of backward/forward conversion between dP and mask
    if threshold is not None and threshold > 0 and flows is not None:
        M0 = remove_bad_flow_masks(M0, flows, threshold=threshold)
        _,M0 = np.unique(M0, return_inverse=True)
        M0 = np.reshape(M0, shape0).astype(np.int32)

    return M0

def fill_holes(masks, min_size=15):
    """ fill holes in masks (2D) and discard masks smaller than min_size
    
    fill holes in each mask using scipy.ndimage.morphology.binary_fill_holes
    
    Parameters
    ----------------

    masks: int, 2D array
        labelled masks, 0=NO masks; 1,2,...=mask labels,
        size [Ly x Lx]

    min_size: int (optional, default 15)
        minimum number of pixels per mask

    Returns
    ---------------

    masks: int, 2D array
        masks with holes filled and masks smaller than min_size removed, 
        0=NO masks; 1,2,...=mask labels,
        size [Ly x Lx]
    
    """

    slices = scipy.ndimage.find_objects(masks)
    i = 0
    for sr, sc in slices:
        msk = masks[sr, sc] == (i+1)
        msk = scipy.ndimage.morphology.binary_fill_holes(msk)
        sm = np.logical_and(msk, ~skimage.morphology.remove_small_objects(msk, min_size=min_size, connectivity=1))
        masks[sr, sc][msk] = (i+1)
        masks[sr, sc][sm] = 0
        i+=1
    return masks
