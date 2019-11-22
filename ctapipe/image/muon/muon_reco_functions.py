import logging
import warnings

import numpy as np
from astropy import log
from astropy import units as u
from astropy.coordinates import Angle, SkyCoord, AltAz
from astropy.utils.decorators import deprecated

from ctapipe.coordinates import CameraFrame, TelescopeFrame
from ctapipe.image.cleaning import tailcuts_clean
from ctapipe.image.muon.features import ring_containment
from ctapipe.image.muon.features import ring_completeness
from ctapipe.image.muon.features import npix_above_threshold
from ctapipe.image.muon.features import npix_composing_ring
from ctapipe.image.muon.muon_integrator import MuonLineIntegrate
from ctapipe.image.muon.muon_ring_finder import MuonRingFitter

logger = logging.getLogger(__name__)


def transform_pixel_coords_from_meter_to_deg(x, y, foc_len, fast_but_bad=False):
    if fast_but_bad:
        delta_alt = np.rad2deg((x / foc_len).value) * u.deg
        delta_az = np.rad2deg((y / foc_len).value) * u.deg

    else:
        pixel_coords_in_telescope_frame = SkyCoord(
            x=x,
            y=y,
            frame=CameraFrame(focal_length=foc_len)
        ).transform_to(TelescopeFrame())
        delta_az = pixel_coords_in_telescope_frame.delta_az.deg
        delta_alt = pixel_coords_in_telescope_frame.delta_alt.deg

    return delta_az, delta_alt

def calc_nom_dist(ring_fit):
    nom_dist = np.sqrt(
        (ring_fit.ring_center_x)**2 +
        (ring_fit.ring_center_y)**2
    )
    return nom_dist

def calc_dist_and_ring_dist(x, y, ring_fit, parameter=0.4):
    dist = np.sqrt(
        (x - ring_fit.ring_center_x)**2 +
        (y - ring_fit.ring_center_y)**2
    )
    ring_dist = np.abs(dist - ring_fit.ring_radius)
    dist_mask = ring_dist < ring_fit.ring_radius * parameter

    return dist, ring_dist, dist_mask


def generate_muon_cuts_by_telescope_name():
    names = ['LST_LST_LSTCam', 'MST_MST_NectarCam', 'MST_MST_FlashCam', 'MST_SCT_SCTCam',
             'SST_1M_DigiCam', 'SST_GCT_CHEC', 'SST_ASTRI_ASTRICam', 'SST_ASTRI_CHEC']
    tail_cuts = [(5, 7), (5, 7), (10, 12), (5, 7),
                (5, 7), (5, 7), (5, 7), (5, 7)]  # 10, 12?
    impact = [(0.2, 0.9), (0.1, 0.95), (0.2, 0.9), (0.2, 0.9),
              (0.1, 0.95), (0.1, 0.95), (0.1, 0.95), (0.1, 0.95)] # in units of mirror radii
    ringwidth = [(0.04, 0.08), (0.02, 0.1), (0.01, 0.1), (0.02, 0.1),
                 (0.01, 0.5), (0.02, 0.2), (0.02, 0.2), (0.02, 0.2)] * u.deg
    total_pix = [1855., 1855., 1764., 11328., 1296., 2048., 2368., 2048]
    # 8% (or 6%) as limit
    min_pix = [148., 148., 141., 680., 104., 164., 142., 164]
    # Need to either convert from the pixel area in m^2 or check the camera specs
    ang_pixel_width = [0.1, 0.2, 0.18, 0.067, 0.24, 0.2, 0.17, 0.2, 0.163] * u.deg
    # Found from TDRs (or the pixel area)
    hole_rad = [0.308 * u.m, 0.244 * u.m, 0.244 * u.m,
                4.3866 * u.m, 0.160 * u.m, 0.130 * u.m,
                0.171 * u.m, 0.171 * u.m]  # Assuming approximately spherical hole
    cam_rad = [2.26, 3.96, 3.87, 4., 4.45, 2.86, 5.25, 2.86] * u.deg
    # Above found from the field of view calculation
    sec_rad = [0. * u.m, 0. * u.m, 0. * u.m, 2.7 * u.m,
               0. * u.m, 1. * u.m, 1.8 * u.m, 1.8 * u.m]
    sct = [False, False, False, True, False, True, True, True]


    muon_cuts = {'Name': names, 'tail_cuts': tail_cuts, 'Impact': impact,
                 'RingWidth': ringwidth, 'total_pix': total_pix,
                 'min_pix': min_pix, 'CamRad': cam_rad, 'SecRad': sec_rad,
                 'SCT': sct, 'AngPixW': ang_pixel_width, 'HoleRad': hole_rad}

    muon_cuts_list_of_dicts = [
        {k:v for k,v in zip(muon_cuts.keys(), values)}
        for values in zip(*muon_cuts.values())
    ]
    muon_cuts_by_name = {mc['Name']:mc for mc in muon_cuts_list_of_dicts}

    # replace tail_cuts tuples with more descriptive dicts.
    for muon_cut in muon_cuts_by_name.values():
        muon_cut['tail_cuts'] = {
            'picture_thresh': muon_cut['tail_cuts'][0],
            'boundary_thresh': muon_cut['tail_cuts'][1],
        }

    return muon_cuts_by_name

def is_ring_good(cleaned_image, ring_fit, muon_cut):
    '''this is applying some cuts
    I have no idea, if all of these cuts are equally important
    for what follows after these cuts, I hope they are.

    I also do not know if these thresholds need to be user
    configurable or if they are somehow fundamental.

    I have the feeling this part here is a problem.
    '''
    number_of_pixels_above_picture_thresh = npix_above_threshold(
        cleaned_image, muon_cut['tail_cuts']['picture_thresh']
    )

    is_enough_pixels_over_picture_thresh = (
        number_of_pixels_above_picture_thresh > 0.1 * muon_cut['min_pix']
    )

    number_of_non_zero_pixels = npix_composing_ring(cleaned_image)
    is_enough_pixels_in_cleaned_image = (
        number_of_non_zero_pixels > muon_cut['min_pix']
    )

    is_ring_center_inside_camera = (
        calc_nom_dist(ring_fit) < muon_cut['CamRad']
    )

    return (
        is_enough_pixels_over_picture_thresh and
        is_enough_pixels_in_cleaned_image and
        is_ring_center_inside_camera and
        ring_fit.ring_radius > 1. * u.deg and
        ring_fit.ring_radius < 1.5 * u.deg
    )


def do_multi_ring_fit(muon_ring_fit, x, y, image, clean_mask):
    # 1st fit
    ring_fit = muon_ring_fit(x, y, image, clean_mask)
    dist, ring_dist, dist_mask = calc_dist_and_ring_dist(x, y, ring_fit)
    mask = clean_mask * dist_mask
    # 2nd fit
    ring_fit = muon_ring_fit(x, y, image, mask)
    dist, ring_dist, dist_mask = calc_dist_and_ring_dist(x, y, ring_fit)
    mask *= dist_mask
    # 3rd fit
    ring_fit = muon_ring_fit(x, y, image, mask)
    dist, ring_dist, dist_mask = calc_dist_and_ring_dist(x, y, ring_fit)
    mask *= dist_mask

    return ring_fit, mask

def calc_muon_intensity_parameters(x, y, image, mask, ring_fit, ctel):
    '''
    Parameters
    ----------

    x: ndarray in degrees
        x-position of the pixels in the camera
    y: ndarray in degrees
        y-position of the pixels in the camera
    image: ndarray 1D
        charge of the pixels in the camera
    mask: ndarray boolean, shape like image
        true if pixel is likely to be on muon ring

    ring_fit: MuonParameterContainer
        result of previous ring fit

    ctel: MuonLineIntegrate
        to be used in here.
    '''

    muonintensityoutput = ctel.fit_muon(
        ring_fit.ring_center_x,
        ring_fit.ring_center_y,
        ring_fit.ring_radius,
        x[mask],
        y[mask],
        image[mask]
    )

    muonintensityoutput.mask = mask

    muonintensityoutput.ring_completeness = ring_completeness(
        x[mask],
        y[mask],
        image[mask],
        ring_fit.ring_radius,
        ring_fit.ring_center_x,
        ring_fit.ring_center_y,
        threshold=30,
        bins=30)
    muonintensityoutput.ring_size = np.sum(mask)

    dist_ringwidth_mask = ring_dist < muonintensityoutput.ring_width
    pix_ringwidth_im = image * dist_ringwidth_mask

    muonintensityoutput.ring_pix_completeness = (
        (pix_ringwidth_im[dist_ringwidth_mask] > tailcuts['picture_thresh']).sum()
        / dist_ringwidth_mask.sum()
    )

    return muonintensityoutput

def analyze_muon_event(event):
    """
    Generic muon event analyzer.

    Parameters
    ----------
    event : ctapipe dl1 event container


    Returns
    -------
    ring_fit, muonintensityparam : MuonRingParameter
    and MuonIntensityParameter container event

    """
    muon_cuts_by_name = generate_muon_cuts_by_telescope_name()

    output = []
    for telid in event.dl0.tels_with_data:
        image = event.dl1.tel[telid].image
        teldes = event.inst.subarray.tel[telid]
        foc_len = teldes.optics.equivalent_focal_length
        geom = teldes.camera
        optics = teldes.optics
        mirror_radius = optics.mirror_radius
        x, y = geom.pix_x, geom.pix_y

        muon_cut = muon_cuts_by_name[str(teldes)]
        tailcuts = muon_cut['tail_cuts']
        clean_mask = tailcuts_clean(geom, image, **tailcuts)

        x, y = transform_pixel_coords_from_meter_to_deg(
            x, y, foc_len, fast_but_bad=True)

        muon_ring_fit = MuonRingFitter(fit_method="chaudhuri_kundu")
        ctel = MuonLineIntegrate(
            mirror_radius,
            hole_radius=muon_cut['HoleRad'],
            pixel_width=muon_cut['AngPixW'],
            sct_flag=muon_cut['SCT'],
            secondary_radius=muon_cut['SecRad'],
        )


        if not np.any(clean_mask):  # early bail out - safes time
            continue

        ring_fit, mask = do_multi_ring_fit(
            muon_ring_fit, x, y, image, clean_mask
        )
        ring_fit.tel_id = telid
        ring_fit.obs_id = event.dl0.obs_id
        ring_fit.event_id = event.dl0.event_id

        ring_fit.ring_containment = ring_containment(
            ring_fit.ring_radius,
            muon_cut['CamRad'],
            ring_fit.ring_center_x,
            ring_fit.ring_center_y
        )
        result = {
            'MuonRingParams': ring_fit,
            'mirror_radius': mirror_radius,
        }
        if is_ring_good(image * mask, ring_fit, muon_cut):

            muonintensityoutput = calc_muon_intensity_parameters(
                x, y, image, mask, ring_fit
            )
            muonintensityoutput.tel_id = telid
            muonintensityoutput.obs_id = event.dl0.obs_id
            muonintensityoutput.event_id = event.dl0.event_id

            conditions = [
                muonintensityoutput.impact_parameter <
                muon_cut['Impact'][1] * mirror_radius,

                muonintensityoutput.impact_parameter
                > muon_cut['Impact'][0] * mirror_radius,

                muonintensityoutput.ring_width
                < muon_cut['RingWidth'][1],

                muonintensityoutput.ring_width
                > muon_cut['RingWidth'][0]
            ]

            result.update({
                'MuonIntensityParams': muonintensityoutput,
                'muon_found': all(conditions),
            })

        output.append(result)

    return output
