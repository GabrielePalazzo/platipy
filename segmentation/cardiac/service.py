"""
Service to run multi-atlas based cardiac segmentation.

Rob Finnegan
"""
import os
import datetime

import SimpleITK as sitk
from loguru import logger
# import pydicom

from impit.framework import app, DataObject

# from impit.dicom.nifti_to_rtstruct.convert import convert_nifti
from impit.segmentation.atlas.registration import (
    initial_registration,
    transform_propagation,
    fast_symmetric_forces_demons_registration,
    apply_field,
)

from impit.segmentation.atlas.label import (
    compute_weight_map,
    combine_labels,
    process_probability_image,
)

from impit.segmentation.atlas.iterative_atlas_removal import IAR

from .cardiac import (
    AutoLungSegment,
    CropImage,
    vesselSplineGeneration,
)


CARDIAC_SETTINGS_DEFAULTS = {
    "outputFormat": "Auto_{0}.nii.gz",
    "atlasSettings": {
        "atlasIdList": ["08", "11", "12", "13", "14"],
        "atlasStructures": ["WHOLEHEART", "LANTDESCARTERY"],
        # For development, run: 'export ATLAS_PATH=/atlas/path'
        "atlasPath": os.environ["ATLAS_PATH"],
    },
    "lungMaskSettings": {
        "coronalExpansion": 15,
        "axialExpansion": 5,
        "sagittalExpansion": 0,
        "lowerNormalisedThreshold": -0.1,
        "upperNormalisedThreshold": 0.4,
        "voxelCountThreshold": 5e4,
    },
    "rigidSettings": {
        "initialReg": "Affine",
        "options": {
            "shrinkFactors": [8, 4, 2, 1],
            "smoothSigmas": [8, 4, 1, 0],
            "samplingRate": 0.25,
            "finalInterp": sitk.sitkBSpline,
        },
        "trace": True,
        "guideStructure": False,
    },
    "deformableSettings": {
        "resolutionStaging": [16, 4, 2, 1],
        "iterationStaging": [20, 10, 10, 10],
        "ncores": 8,
        "trace": True,
    },
    "IARSettings": {
        "referenceStructure": "WHOLEHEART",
        "smoothDistanceMaps": True,
        "smoothSigma": 1,
        "zScoreStatistic": "MAD",
        "outlierMethod": "IQR",
        "outlierFactor": 1.5,
        "minBestAtlases": 4,
    },
    "labelFusionSettings": {"voteType": "local", "optimalThreshold": {"WHOLEHEART": 0.44}},
    "vesselSpliningSettings": {
        "vesselNameList": ["LANTDESCARTERY"],
        "vesselRadius_mm": {"LANTDESCARTERY": 2.2},
        "spliningDirection": {"LANTDESCARTERY": "z"},
        "stopCondition": {"LANTDESCARTERY": "count"},
        "stopConditionValue": {"LANTDESCARTERY": 1},
    },
}


@app.register("Cardiac Segmentation", default_settings=CARDIAC_SETTINGS_DEFAULTS)
def cardiac_service(data_objects, working_dir, settings):
    """
    Implements the impit framework to provide cardiac atlas based segmentation.
    """

    logger.info("Running Cardiac Segmentation")
    logger.info("Using settings: " + str(settings))

    output_objects = []
    for data_object in data_objects:
        logger.info("Running on data object: " + data_object.path)

        # Read the image series
        load_path = data_object.path
        if data_object.type == "DICOM":
            load_path = sitk.ImageSeriesReader().GetGDCMSeriesFileNames(data_object.path)

        img = sitk.ReadImage(load_path)

        """
        Initialisation - Read in atlases
        - image files
        - structure files

            Atlas structure:
            'ID': 'Original': 'CT Image'    : sitk.Image
                              'Struct A'    : sitk.Image
                              'Struct B'    : sitk.Image
                  'RIR'     : 'CT Image'    : sitk.Image
                              'Transform'   : transform parameter map
                              'Struct A'    : sitk.Image
                              'Struct B'    : sitk.Image
                  'DIR'     : 'CT Image'    : sitk.Image
                              'Transform'   : displacement field transform
                              'Weight Map'  : sitk.Image
                              'Struct A'    : sitk.Image
                              'Struct B'    : sitk.Image


        """

        logger.info("")
        # Settings
        atlas_path = settings["atlasSettings"]["atlasPath"]
        atlas_id_list = settings["atlasSettings"]["atlasIdList"]
        atlas_structures = settings["atlasSettings"]["atlasStructures"]

        atlas_set = {}
        for atlas_id in atlas_id_list:
            atlas_set[atlas_id] = {}
            atlas_set[atlas_id]["Original"] = {}

            atlas_set[atlas_id]["Original"]["CT Image"] = sitk.ReadImage(
                "{0}/Case_{1}/Images/Case_{1}_CROP.nii.gz".format(atlas_path, atlas_id)
            )

            for struct in atlas_structures:
                atlas_set[atlas_id]["Original"][struct] = sitk.ReadImage(
                    "{0}/Case_{1}/Structures/Case_{1}_{2}_CROP.nii.gz".format(
                        atlas_path, atlas_id, struct
                    )
                )

        """
        Step 1 - Automatic cropping using the lung volume
        - Airways are segmented
        - A bounding box is defined
        - Potential expansion of the bounding box to ensure entire heart volume is enclosed
        - Target image is cropped
        """
        # Settings
        sagittal_expansion = settings["lungMaskSettings"]["sagittalExpansion"]
        coronal_expansion = settings["lungMaskSettings"]["coronalExpansion"]
        axial_expansion = settings["lungMaskSettings"]["axialExpansion"]

        lower_normalised_threshold = settings["lungMaskSettings"]["lowerNormalisedThreshold"]
        upper_normalised_threshold = settings["lungMaskSettings"]["upperNormalisedThreshold"]
        voxel_count_threshold = settings["lungMaskSettings"]["voxelCountThreshold"]

        # Get the bounding box containing the lungs
        lung_bounding_box, lung_mask_original = AutoLungSegment(
            img,
            l=lower_normalised_threshold,
            u=upper_normalised_threshold,
            NPthresh=voxel_count_threshold,
        )

        # Add an optional expansion
        sag0 = max([lung_bounding_box[0] - sagittal_expansion, 0])
        cor0 = max([lung_bounding_box[1] - coronal_expansion, 0])
        ax0 = max([lung_bounding_box[2] - axial_expansion, 0])

        sag_d = min([lung_bounding_box[3] + sagittal_expansion, img.GetSize()[0] - sag0])
        cor_d = min([lung_bounding_box[4] + coronal_expansion, img.GetSize()[1] - cor0])
        ax_d = min([lung_bounding_box[5] + axial_expansion, img.GetSize()[2] - ax0])

        crop_box = (sag0, cor0, ax0, sag_d, cor_d, ax_d)

        # Crop the image down
        img_crop = CropImage(img, crop_box)

        # Crop the lung mask - it may be used for structure guided registration
        lung_mask = CropImage(lung_mask_original, crop_box)

        # TODO: We should check here that the lung segmentation has worked, otherwise we need
        # another option!
        # For example, translation registration with a pre-cropped image

        """
        Step 2 - Rigid registration of target images
        - Individual atlas images are registered to the target
        - The transformation is used to propagate the labels onto the target
        """
        initial_reg = settings["rigidSettings"]["initialReg"]
        rigid_options = settings["rigidSettings"]["options"]
        trace = settings["rigidSettings"]["trace"]
        guide_structure = settings["rigidSettings"]["guideStructure"]

        for atlas_id in atlas_id_list:
            # Register the atlases
            atlas_set[atlas_id]["RIR"] = {}
            atlas_image = atlas_set[atlas_id]["Original"]["CT Image"]

            if guide_structure:
                atlas_struct = atlas_set[atlas_id]["Original"][guide_structure]
            else:
                atlas_struct = False

            rigid_image, initial_tfm = initial_registration(
                img_crop,
                atlas_image,
                moving_structure=atlas_struct,
                options=rigid_options,
                trace=trace,
                reg_method=initial_reg,
            )

            # Save in the atlas dict
            atlas_set[atlas_id]["RIR"]["CT Image"] = rigid_image
            atlas_set[atlas_id]["RIR"]["Transform"] = initial_tfm

            # sitk.WriteImage(rigidImage, f'./RR_{atlas_id}.nii.gz')

            for struct in atlas_structures:
                input_struct = atlas_set[atlas_id]["Original"][struct]
                atlas_set[atlas_id]["RIR"][struct] = transform_propagation(
                    img_crop, input_struct, initial_tfm, structure=True, interp=sitk.sitkLinear
                )

        """
        Step 3 - Deformable image registration
        - Using Fast Symmetric Diffeomorphic Demons
        """
        # Settings
        resolution_staging = settings["deformableSettings"]["resolutionStaging"]
        iteration_staging = settings["deformableSettings"]["iterationStaging"]
        ncores = settings["deformableSettings"]["ncores"]
        trace = settings["deformableSettings"]["trace"]

        for atlas_id in atlas_id_list:
            # Register the atlases
            atlas_set[atlas_id]["DIR"] = {}
            atlas_image = atlas_set[atlas_id]["RIR"]["CT Image"]
            deform_image, deform_field = fast_symmetric_forces_demons_registration(
                img_crop,
                atlas_image,
                resolution_staging=resolution_staging,
                iteration_staging=iteration_staging,
                ncores=ncores,
                trace=trace,
            )

            # Save in the atlas dict
            atlas_set[atlas_id]["DIR"]["CT Image"] = deform_image
            atlas_set[atlas_id]["DIR"]["Transform"] = deform_field

            # sitk.WriteImage(deformImage, f'./DIR_{atlas_id}.nii.gz')

            for struct in atlas_structures:
                input_struct = atlas_set[atlas_id]["RIR"][struct]
                atlas_set[atlas_id]["DIR"][struct] = apply_field(
                    input_struct, deform_field, structure=True, interp=sitk.sitkLinear
                )

        """
        Step 4 - Iterative atlas removal
        - This is an automatic process that will attempt to remove inconsistent atlases from the entire set

        """

        # Compute weight maps
        for atlas_id in atlas_id_list:
            atlas_image = atlas_set[atlas_id]["DIR"]["CT Image"]
            weight_map = compute_weight_map(img_crop, atlas_image)
            atlas_set[atlas_id]["DIR"]["Weight Map"] = weight_map

        reference_structure = settings["IARSettings"]["referenceStructure"]
        smooth_distance_maps = settings["IARSettings"]["smoothDistanceMaps"]
        smooth_sigma = settings["IARSettings"]["smoothSigma"]
        z_score_statistic = settings["IARSettings"]["zScoreStatistic"]
        outlier_method = settings["IARSettings"]["outlierMethod"]
        outlier_factor = settings["IARSettings"]["outlierFactor"]
        min_best_atlases = settings["IARSettings"]["minBestAtlases"]

        atlas_set = IAR(
            atlas_set=atlas_set,
            structure_name=reference_structure,
            smooth_maps=smooth_distance_maps,
            smooth_sigma=smooth_sigma,
            z_score=z_score_statistic,
            outlier_method=outlier_method,
            min_best_atlases=min_best_atlases,
            n_factor=outlier_factor,
            log_file="IAR_{0}.log".format(datetime.datetime.now()),
            debug=False,
            iteration=0,
            single_step=False,
        )

        """
        Step 4 - Vessel Splining

        """

        vessel_name_list = settings["vesselSpliningSettings"]["vesselNameList"]
        vessel_radius_mm = settings["vesselSpliningSettings"]["vesselRadius_mm"]
        splining_direction = settings["vesselSpliningSettings"]["spliningDirection"]
        stop_condition = settings["vesselSpliningSettings"]["stopCondition"]
        stop_condition_value = settings["vesselSpliningSettings"]["stopConditionValue"]

        segmented_vessel_dict = vesselSplineGeneration(
            atlas_set,
            vessel_name_list,
            vesselRadiusDict=vessel_radius_mm,
            stopConditionTypeDict=stop_condition,
            stopConditionValueDict=stop_condition_value,
            scanDirectionDict=splining_direction,
        )

        """
        Step 5 - Label Fusion
        """
        combined_label_dict = combine_labels(atlas_set, atlas_structures)

        """
        Step 6 - Paste the cropped structure into the original image space
        """

        output_format = settings["outputFormat"]

        template_im = sitk.Cast((img * 0), sitk.sitkUInt8)

        vote_structures = settings["labelFusionSettings"]["optimalThreshold"].keys()

        for structure_name in vote_structures:
            optimal_threshold = settings["labelFusionSettings"]["optimalThreshold"][structure_name]
            binary_struct = process_probability_image(
                combined_label_dict[structure_name], optimal_threshold
            )
            paste_img = sitk.Paste(
                template_im, binary_struct, binary_struct.GetSize(), (0, 0, 0), (sag0, cor0, ax0)
            )

            # Write the mask to a file in the working_dir
            mask_file = os.path.join(working_dir, output_format.format(structure_name))
            sitk.WriteImage(paste_img, mask_file)

            # Create the output Data Object and add it to the list of output_objects
            output_data_object = DataObject(type="FILE", path=mask_file, parent=data_object)
            output_objects.append(output_data_object)

        for structure_name in vessel_name_list:
            binary_struct = segmented_vessel_dict[structure_name]
            paste_img = sitk.Paste(
                template_im, binary_struct, binary_struct.GetSize(), (0, 0, 0), (sag0, cor0, ax0)
            )

            # Write the mask to a file in the working_dir
            mask_file = os.path.join(working_dir, output_format.format(structure_name))
            sitk.WriteImage(paste_img, mask_file)

            # Create the output Data Object and add it to the list of output_objects
            output_data_object = DataObject(type="FILE", path=mask_file, parent=data_object)
            output_objects.append(output_data_object)

        # If the input was a DICOM, then we can use it to generate an output RTStruct
        # if data_object.type == 'DICOM':

        #     dicom_file = load_path[0]
        #     logger.info('Will write Dicom using file: {0}'.format(dicom_file))
        #     masks = {settings['outputContourName']: mask_file}

        #     # Use the image series UID for the file of the RTStruct
        #     suid = pydicom.dcmread(dicom_file).SeriesInstanceUID
        #     output_file = os.path.join(working_dir, 'RS.{0}.dcm'.format(suid))

        #     # Use the convert nifti function to generate RTStruct from nifti masks
        #     convert_nifti(dicom_file, masks, output_file)

        #     # Create the Data Object for the RTStruct and add it to the list
        #     do = DataObject(type='DICOM', path=output_file, parent=d)
        #     output_objects.append(do)

        #     logger.info('RTStruct generated')

    return output_objects


if __name__ == "__main__":

    # Run app by calling "python sample.py" from the command line

    DICOM_LISTENER_PORT = 7777
    DICOM_LISTENER_AETITLE = "SAMPLE_SERVICE"

    app.run(
        debug=True,
        host="0.0.0.0",
        port=8000,
        dicom_listener_port=DICOM_LISTENER_PORT,
        dicom_listener_aetitle=DICOM_LISTENER_AETITLE,
    )
