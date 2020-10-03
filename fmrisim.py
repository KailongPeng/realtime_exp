#  Copyright 2016 Intel Corporation
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

"""fMRI Simulator

Simulate fMRI data for a single subject.

This code provides a set of functions necessary to produce realistic
simulations of fMRI data. There are two main steps: characterizing the
signal and generating the noise model, which are then combined to simulate
brain data. Tools are included to support the creation of different types
of signal, such as region specific differences in univariate
activity. To create the noise model the parameters can either be set
manually or can be estimated from real fMRI data with reasonable accuracy (
works best when fMRI data has not been preprocessed)

Functions:

generate_signal
Create a volume with activity, of a specified shape and either multivariate
or univariate pattern, in a specific region to represent the signal in the
neural data.

generate_stimfunction
Create a timecourse of the signal activation. This can be specified using event
onsets and durations from a timing file. This is the time course before
convolution and therefore can be at any temporal precision.

export_3_column:
Generate a three column timing file that can be used with software like FSL
to represent event event onsets and duration

export_epoch_file:
Generate an epoch file from the time course which can be used as an input to
brainiak functions

convolve_hrf
Convolve the signal timecourse with the  HRF to model the expected evoked
activity

apply_signal
Combine the signal volume with the HRF, thus giving the signal the temporal
properties of the HRF (such as smoothing and lag)

calc_noise
Estimate the noise properties of a given fMRI volume. Prominently, estimate
the smoothing and SFNR of the data

generate_noise
Create the noise for this run. This creates temporal, spatial task and white
noise. Various parameters can be tuned depending on need

mask_brain
Create a mask volume that has similar contrast as an fMRI image. Defaults to
use an MNI grey matter atlas but any image can be supplied to create an
estimate.

plot_brain
Display the brain, timepoint by timepoint, with above threshold voxels
highlighted against the outline of the brain.


 Authors:
 Cameron Ellis (Princeton) 2016-2017
 Chris Baldassano (Princeton) 2016-2017
"""
import logging

from itertools import product
import math
import numpy as np
from pkg_resources import resource_stream
from scipy import stats
from scipy import signal
import scipy.ndimage as ndimage

__all__ = [
    "generate_signal",
    "generate_stimfunction",
    "export_3_column",
    "export_epoch_file",
    "convolve_hrf",
    "apply_signal",
    "calc_noise",
    "generate_noise",
    "mask_brain",
    "plot_brain",
]

logger = logging.getLogger(__name__)


def _generate_feature(feature_type,
                      feature_size,
                      signal_magnitude,
                      thickness=1):
    """Generate features corresponding to signal

    Generate a single feature, that can be inserted into the signal volume.
    A feature is a region of activation with a specific shape such as cube
    or ring

    Parameters
    ----------

    feature_type : str
        What shape signal is being inserted? Options are 'cube',
        'loop' (aka ring), 'cavity' (aka hollow sphere), 'sphere'.

    feature_size : int
        How big is the signal in diameter?

    signal_magnitude : float
        Set the signal size, a value of 1 means the signal is one standard
        deviation of the noise

    thickness : int
        How thick is the surface of the loop/cavity

    Returns
    ----------

    signal : 3 dimensional array
        The volume representing the signal

    """

    # If the size is equal to or less than 2 then all features are the same
    if feature_size <= 2:
        feature_type = 'cube'

    # What kind of signal is it?
    if feature_type == 'cube':

        # Preset the size of the signal
        signal = np.ones((feature_size, feature_size, feature_size))

    elif feature_type == 'loop':

        # First make a cube of zeros
        signal = np.zeros((feature_size, feature_size, feature_size))

        # Make a mesh grid of the space
        seq = np.linspace(0, feature_size - 1,
                          feature_size)
        xx, yy = np.meshgrid(seq, seq)

        # Make a disk corresponding to the whole mesh grid
        xxmesh = (xx - ((feature_size - 1) / 2)) ** 2
        yymesh = (yy - ((feature_size - 1) / 2)) ** 2
        disk = xxmesh + yymesh

        # What are the limits of the rings being made
        outer_lim = disk[int((feature_size - 1) / 2), 0]
        inner_lim = disk[int((feature_size - 1) / 2), thickness]

        # What is the outer disk
        outer = disk <= outer_lim

        # What is the inner disk
        inner = disk <= inner_lim

        # Subtract the two disks to get a loop
        loop = outer != inner

        # Check if the loop is a disk
        if np.all(inner is False):
            logger.warning('Loop feature reduces to a disk because the loop '
                           'is too thick')

        # If there is complete overlap then make the signal just the
        #  outer one
        if np.all(loop is False):
            loop = outer

        # store the loop
        signal[0:feature_size, 0:feature_size, int(np.round(feature_size /
                                                            2))] = loop

    elif feature_type == 'sphere' or feature_type == 'cavity':

        # Make a mesh grid of the space
        seq = np.linspace(0, feature_size - 1,
                          feature_size)
        xx, yy, zz = np.meshgrid(seq, seq, seq)

        # Make a disk corresponding to the whole mesh grid
        signal = ((xx - ((feature_size - 1) / 2)) ** 2 +
                  (yy - ((feature_size - 1) / 2)) ** 2 +
                  (zz - ((feature_size - 1) / 2)) ** 2)

        # What are the limits of the rings being made
        outer_lim = signal[int((feature_size - 1) / 2), int((feature_size -
                                                             1) / 2), 0]
        inner_lim = signal[int((feature_size - 1) / 2), int((feature_size -
                                                             1) / 2),
                           thickness]

        # Is the signal a sphere or a cavity?
        if feature_type == 'sphere':
            signal = signal <= outer_lim

        else:
            # Get the inner and outer sphere
            outer = signal <= outer_lim
            inner = signal <= inner_lim

            # Subtract the two disks to get a loop
            signal = outer != inner

            # Check if the cavity is a sphere
            if np.all(inner is False):
                logger.warning('Cavity feature reduces to a sphere because '
                               'the cavity is too thick')

            # If there is complete overlap then make the signal just the
            #  outer one
            if np.all(signal is False):
                signal = outer

    # Assign the signal magnitude
    signal = signal * signal_magnitude

    # Return the signal
    return signal


def _insert_idxs(feature_centre, feature_size, dimensions):
    """Returns the indices of where to put the signal into the signal volume

    Parameters
    ----------

    feature_centre : list, int
        List of coordinates for the centre location of the signal

    feature_size : list, int
        How big is the signal's diameter.

    dimensions : 3 length array, int
        What are the dimensions of the volume you wish to create


    Returns
    ----------
    x_idxs : tuple
        The x coordinates of where the signal is to be inserted

    y_idxs : tuple
        The y coordinates of where the signal is to be inserted

    z_idxs : tuple
        The z coordinates of where the signal is to be inserted

    """

    # Set up the indexes within which to insert the signal
    x_idx = [int(feature_centre[0] - (feature_size / 2)) + 1,
             int(feature_centre[0] - (feature_size / 2) +
                 feature_size) + 1]
    y_idx = [int(feature_centre[1] - (feature_size / 2)) + 1,
             int(feature_centre[1] - (feature_size / 2) +
                 feature_size) + 1]
    z_idx = [int(feature_centre[2] - (feature_size / 2)) + 1,
             int(feature_centre[2] - (feature_size / 2) +
                 feature_size) + 1]

    # Check for out of bounds
    # Min Boundary
    if 0 > x_idx[0]:
        x_idx[0] = 0
    if 0 > y_idx[0]:
        y_idx[0] = 0
    if 0 > z_idx[0]:
        z_idx[0] = 0

    # Max Boundary
    if dimensions[0] < x_idx[1]:
        x_idx[1] = dimensions[0]
    if dimensions[1] < y_idx[1]:
        y_idx[1] = dimensions[1]
    if dimensions[2] < z_idx[1]:
        z_idx[1] = dimensions[2]

    # Return the idxs for data
    return x_idx, y_idx, z_idx


def generate_signal(dimensions,
                    feature_coordinates,
                    feature_size,
                    feature_type,
                    signal_magnitude=[1],
                    signal_constant=1,
                    ):
    """Generate volume containing signal

    Generate signal, of a specific shape in specific regions, for a single
    volume. This will then be convolved with the HRF across time

    Parameters
    ----------

    dimensions : 1d array, ndarray
        What are the dimensions of the volume you wish to create

    feature_coordinates : multidimensional array
        What are the feature_coordinates of the signal being created.
        Be aware of clipping: features far from the centre of the
        brain will be clipped. If you wish to have multiple features
        then list these as a features x 3 array. To create a feature of
        a unique shape then supply all the individual
        feature_coordinates of the shape and set the feature_size to 1.

    feature_size : list, int
        How big is the signal. If feature_coordinates=1 then only one value is
        accepted, if feature_coordinates>1 then either one value must be
        supplied or m values

    feature_type : list, string
        What feature_type of signal is being inserted? Options are cube,
        loop, cavity, sphere. If feature_coordinates = 1 then
        only one value is accepted, if feature_coordinates > 1 then either
        one value must be supplied or m values

    signal_magnitude : list, float
        What is the (average) magnitude of the signal being generated? A
        value of 1 means that the signal is one standard deviation from the
        noise

    signal_constant : list, bool
        Is the signal constant across the feature (for univariate activity)
        or is it a random pattern of a given magnitude across the feature (for
        multivariate activity)

    Returns
    ----------
    volume_signal : 3 dimensional array, float
        Creates a single volume containing the signal

    """

    # Preset the volume
    volume_signal = np.zeros(dimensions)

    feature_quantity = round(feature_coordinates.shape[0])

    # If there is only one feature_size value then make sure to duplicate it
    # for all signals
    if len(feature_size) == 1:
        feature_size = feature_size * feature_quantity

    # Do the same for feature_type
    if len(feature_type) == 1:
        feature_type = feature_type * feature_quantity

    if len(signal_magnitude) == 1:
        signal_magnitude = signal_magnitude * feature_quantity

    # Iterate through the signals and insert in the data
    for signal_counter in range(feature_quantity):

        # What is the centre of this signal
        if len(feature_size) > 1:
            feature_centre = np.asarray(feature_coordinates[signal_counter, ])
        else:
            feature_centre = np.asarray(feature_coordinates)[0]

        # Generate the feature to be inserted in the volume
        signal = _generate_feature(feature_type[signal_counter],
                                   feature_size[signal_counter],
                                   signal_magnitude[signal_counter],
                                   )

        # If the signal is a random noise pattern then multiply these ones by
        # a noise mask
        if signal_constant == 0:
            signal = signal * np.random.random([feature_size[signal_counter],
                                                feature_size[signal_counter],
                                                feature_size[signal_counter]])

        # Pull out the idxs for where to insert the data
        x_idx, y_idx, z_idx = _insert_idxs(feature_centre,
                                           feature_size[signal_counter],
                                           dimensions)

        # Insert the signal into the Volume
        volume_signal[x_idx[0]:x_idx[1], y_idx[0]:y_idx[1], z_idx[0]:z_idx[
            1]] = signal

    return volume_signal


def generate_stimfunction(onsets,
                          event_durations,
                          total_time,
                          weights=[1],
                          timing_file=None,
                          temporal_resolution=100.0,
                          ):
    """Return the function for the timecourse events

    When do stimuli onset, how long for and to what extent should you
    resolve the fMRI time course. There are two ways to create this, either
    by supplying onset, duration and weight information or by supplying a
    timing file (in the three column format used by FSL).

    Parameters
    ----------

    onsets : list, int
        What are the timestamps (in s) for when an event you want to
        generate onsets?

    event_durations : list, int
        What are the durations (in s) of the events you want to
        generate? If there is only one value then this will be assigned
        to all onsets

    total_time : int
        How long (in s) is the experiment in total.

    weights : list, float
        What is the weight for each event (how high is the box car)? If
        there is only one value then this will be assigned to all onsets

    timing_file : string
        The filename (with path) to a three column timing file (FSL) to
        make the events. Still requires total_time to work

    temporal_resolution : float
        How many elements per second are you modeling for the
        timecourse. This is useful when you want to model the HRF at an
        arbitrarily high resolution (and then downsample to your TR later).

    Returns
    ----------

    stim_function : 1 by timepoint array, float
        The time course of stimulus evoked activation. This has a temporal
        resolution of temporal resolution / 1.0 elements per second

    """

    # If the timing file is supplied then use this to acquire the
    if timing_file is not None:

        # Read in text file line by line
        with open(timing_file) as f:
            text = f.readlines()  # Pull out file as a an array

        # Preset
        onsets = list()
        event_durations = list()
        weights = list()

        # Pull out the onsets, weights and durations, set as a float
        for line in text:
            onset, duration, weight = line.strip().split()

            # Check if the onset is more precise than the temporal resolution
            upsampled_onset = float(onset) * temporal_resolution

            # Because of float precision, the upsampled values might
            # not round as expected .
            # E.g. float('1.001') * 1000 = 1000.99
            if np.allclose(upsampled_onset, np.round(upsampled_onset)) == 0:
                warning = 'Your onset: ' + str(onset) + ' has more decimal ' \
                                                        'points than the ' \
                                                        'specified temporal ' \
                                                        'resolution can ' \
                                                        'resolve. This means' \
                                                        ' that events might' \
                                                        ' be missed. ' \
                                                        'Consider increasing' \
                                                        ' the temporal ' \
                                                        'resolution.'
                logging.warning(warning)

            onsets.append(float(onset))
            event_durations.append(float(duration))
            weights.append(float(weight))

    # If only one duration is supplied then duplicate it for the length of
    # the onset variable
    if len(event_durations) == 1:
        event_durations = event_durations * len(onsets)

    if len(weights) == 1:
        weights = weights * len(onsets)

    # Check files
    if np.max(onsets) > total_time:
        raise ValueError('Onsets outside of range of total time.')

    # Generate the time course as empty, each element is a millisecond by
    # default
    stimfunction = np.zeros((int(round(total_time * temporal_resolution)), 1))

    # Cycle through the onsets
    for onset_counter in list(range(len(onsets))):
        # Adjust for the resolution
        onset_idx = int(np.floor(onsets[onset_counter] * temporal_resolution))

        # Adjust for the resolution
        offset_idx = int(np.floor((onsets[onset_counter] + event_durations[
            onset_counter]) * temporal_resolution))

        # Store the weights
        stimfunction[onset_idx:offset_idx, 0] = [weights[onset_counter]]

    # Shorten the data if it's too long
    if stimfunction.shape[0] > total_time * temporal_resolution:
        stimfunction = stimfunction[0:int(total_time * temporal_resolution), 0]

    return stimfunction


def export_3_column(stimfunction,
                    filename,
                    temporal_resolution=100.0
                    ):
    """ Output a tab separated three column timing file

    This produces a three column tab separated text file, with the three
    columns representing onset time (s), event duration (s) and weight,
    respectively. Useful if you want to run the simulated data through FEAT
    analyses. In a way, this is the reverse of generate_stimfunction

    Parameters
    ----------

    stimfunction : timepoint by 1 array
        The stimulus function describing the time course of events. For
        instance output from generate_stimfunction.

    filename : str
        The name of the three column text file to be output

    temporal_resolution : float
        How many elements per second are you modeling with the
        stimfunction?

    """

    # Iterate through the stim function
    stim_counter = 0
    event_counter = 0
    while stim_counter < stimfunction.shape[0]:

        # Is it an event?
        if stimfunction[stim_counter, 0] != 0:

            # When did the event start?
            event_onset = str(stim_counter / temporal_resolution)

            # The weight of the stimulus
            weight = str(stimfunction[stim_counter, 0])

            # Reset
            event_duration = 0

            # Is the event still ongoing?
            while stimfunction[stim_counter, 0] != 0 & stim_counter <= \
                    stimfunction.shape[0]:

                # Add one millisecond to each duration
                event_duration = event_duration + 1

                # Increment
                stim_counter = stim_counter + 1

            # How long was the event in seconds
            event_duration = str(event_duration / temporal_resolution)

            # Append this row to the data file
            with open(filename, "a") as file:
                file.write(event_onset + '\t' + event_duration + '\t' +
                           weight + '\n')

            # Increment the number of events
            event_counter = event_counter + 1

        # Increment
        stim_counter = stim_counter + 1


def export_epoch_file(stimfunction,
                      filename,
                      tr_duration,
                      temporal_resolution=100.0
                      ):
    """ Output an epoch file, necessary for some inputs into brainiak

    This takes in the time course of stimulus events and outputs the epoch
    file used in Brainiak. The epoch file is a way to structure the timing
    information in fMRI that allows you to flexibly input different stimulus
    sequences. This is a list with each entry a 3d matrix corresponding to a
    participant. The dimensions of the 3d matrix are condition by epoch by time

    Parameters
    ----------

    stimfunction : list of timepoint by condition arrays
        The stimulus function describing the time course of events. Each
        list entry is from a different participant, each row is a different
        timepoint (with the given temporal precision), each column is a
        different condition. export_epoch_file is looking for differences in
        the value of stimfunction to identify the start and end of an
        epoch. If epochs in stimfunction are coded with the same weight and
        there is no time between blocks then export_epoch_file won't be able to
        label them as different epochs

    filename : str
        The name of the three column text file to be output

    tr_duration : float
        How long is each TR in seconds

    temporal_resolution : float
        How many elements per second are you modeling with the
        stimfunction?

    """

    # Cycle through the participants, different entries in the list
    epoch_file = [0] * len(stimfunction)
    for participant_counter in range(len(stimfunction)):

        # What is the time course for the participant (binarized)
        stimfunction_ppt = np.abs(stimfunction[participant_counter]) > 0

        # Cycle through conditions
        conditions = stimfunction_ppt.shape[1]
        for condition_counter in range(conditions):

            # Down sample the stim function
            stride = tr_duration * temporal_resolution
            stimfunction_temp = stimfunction_ppt[:, condition_counter]
            stimfunction_temp = stimfunction_temp[::int(stride)]

            if condition_counter == 0:
                # Calculates the number of event onsets (max of all
                # conditions). This uses changes in value to reflect
                # different epochs. This might be false in some cases (the
                # weight is supposed to unfold over an epoch or there is no
                # break between identically weighted epochs). In such cases
                # this will not work
                weight_change = (np.diff(stimfunction_temp, 1, 0) != 0)
                epochs = int(np.max(np.sum(weight_change, 0)) / 2)

                # Get other information
                trs = stimfunction_temp.shape[0]

                # Make a timing file for this participant
                epoch_file[participant_counter] = np.zeros((conditions,
                                                            epochs, trs))

            epoch_counter = 0
            tr_counter = 0
            while tr_counter < stimfunction_temp.shape[0]:

                # Is it an event?
                if stimfunction_temp[tr_counter] == 1:

                    # Add a one for this TR
                    epoch_file[participant_counter][condition_counter,
                                                    epoch_counter,
                                                    tr_counter] = 1

                    # Find the next non event value
                    end_idx = np.where(stimfunction_temp[tr_counter:] == 0)[
                        0][0]
                    tr_idxs = list(range(tr_counter, tr_counter + end_idx))

                    # Add ones to all the trs within this event time frame
                    epoch_file[participant_counter][condition_counter,
                                                    epoch_counter,
                                                    tr_idxs] = 1

                    # Start from this index
                    tr_counter += end_idx

                    # Increment
                    epoch_counter += 1

                # Increment the counter
                tr_counter += 1

    # Save the file
    np.save(filename, epoch_file)


def _double_gamma_hrf(response_delay=6,
                      undershoot_delay=12,
                      response_dispersion=0.9,
                      undershoot_dispersion=0.9,
                      response_scale=1,
                      undershoot_scale=0.035,
                      temporal_resolution=100.0,
                      ):
    """Create the double gamma HRF with the timecourse evoked activity.
    Default values are based on Glover, 1999 and Walvaert, Durnez,
    Moerkerke, Verdoolaege and Rosseel, 2011

    Parameters
    ----------

    response_delay : float
        How many seconds until the peak of the HRF

    undershoot_delay : float
        How many seconds until the trough of the HRF

    response_dispersion : float
        How wide is the rising peak dispersion

    undershoot_dispersion : float
        How wide is the undershoot dispersion

    response_scale : float
         How big is the response relative to the peak

    undershoot_scale :float
        How big is the undershoot relative to the trough

    scale_function : bool
        Do you want to scale the function to a range of 1

    temporal_resolution : float
        How many elements per second are you modeling for the stimfunction
    Returns
    ----------

    hrf : multi dimensional array
        A double gamma HRF to be used for convolution.

    """

    hrf_length = 30  # How long is the HRF being created

    # How many seconds of the HRF will you model?
    hrf = [0] * int(hrf_length * temporal_resolution)

    # When is the peak of the two aspects of the HRF
    response_peak = response_delay * response_dispersion
    undershoot_peak = undershoot_delay * undershoot_dispersion

    for hrf_counter in list(range(len(hrf) - 1)):

        # Specify the elements of the HRF for both the response and undershoot
        resp_pow = math.pow((hrf_counter / temporal_resolution) /
                            response_peak, response_delay)
        resp_exp = math.exp(-((hrf_counter / temporal_resolution) -
                              response_peak) /
                            response_dispersion)

        response_model = response_scale * resp_pow * resp_exp

        undershoot_pow = math.pow((hrf_counter / temporal_resolution) /
                                  undershoot_peak,
                                  undershoot_delay)
        undershoot_exp = math.exp(-((hrf_counter / temporal_resolution) -
                                    undershoot_peak /
                                    undershoot_dispersion))

        undershoot_model = undershoot_scale * undershoot_pow * undershoot_exp

        # For this time point find the value of the HRF
        hrf[hrf_counter] = response_model - undershoot_model

    return hrf


def convolve_hrf(stimfunction,
                 tr_duration,
                 hrf_type='double_gamma',
                 scale_function=True,
                 temporal_resolution=100.0,
                 ):
    """ Convolve the specified hrf with the timecourse.
    The output of this is a downsampled convolution of the stimfunction and
    the HRF function. If temporal_resolution is 1 / tr_duration then the
    output will be the same length as stimfunction. This time course assumes
    that slice time correction has occurred and all slices have been aligned
    to the middle time point in the TR.

    Be aware that if scaling is on and event durations are less than the
    duration of a TR then the hrf may or may not come out as anticipated.
    This is because very short events would evoke a small absolute response
    after convolution  but if there are only short events and you scale then
    this will look similar to a convolution with longer events. In general
    scaling is useful, which is why it is the default, but be aware of this
    edge case and if it is a concern, set the scale_function to false.

    Parameters
    ----------

    stimfunction : timepoint by timecourse array
        What is the time course of events to be modelled in this
        experiment. This can specify one or more timecourses of events.
        The events can be weighted or binary

    tr_duration : float
        How long (in s) between each volume onset

    hrf_type : str or list
        Takes in a string describing the hrf that ought to be created.
        Can instead take in a vector describing the HRF as it was
        specified by any function. The default is 'double_gamma' in which
        an initial rise and an undershoot are modelled.

    scale_function : bool
        Do you want to scale the function to a range of 1

    temporal_resolution : float
        How many elements per second are you modeling for the stimfunction
    Returns
    ----------

    signal_function : timepoint by timecourse array
        The time course of the HRF convolved with the stimulus function.
        This can have multiple time courses specified as different
        columns in this array.

    """
    # How will stimfunction be resized
    stride = int(temporal_resolution * tr_duration)
    duration = int(stimfunction.shape[0] / stride)

    # Generate the hrf to use in the convolution
    if hrf_type == 'double_gamma':
        hrf = _double_gamma_hrf(temporal_resolution=temporal_resolution)
    elif isinstance(hrf_type, list):
        hrf = hrf_type

    # How many timecourses are there
    list_num = stimfunction.shape[1]

    # Create signal functions for each list in the stimfunction
    for list_counter in range(list_num):

        # Perform the convolution
        signal_temp = np.convolve(stimfunction[:, list_counter], hrf)

        # Down sample the signal function so that it only has one element per
        # TR. This assumes that all slices are collected at the same time,
        # which is often the result of slice time correction. In other
        # words, the output assumes slice time correction
        signal_temp = signal_temp[:duration * stride]
        signal_vox = signal_temp[int(stride / 2)::stride]

        # Scale the function so that the peak response is 1
        if scale_function:
            signal_vox = signal_vox / np.max(signal_vox)

        # Add this function to the stack
        if list_counter == 0:
            signal_function = np.zeros((len(signal_vox), list_num))

        signal_function[:, list_counter] = signal_vox

    return signal_function


def apply_signal(signal_function,
                 volume_signal,
                 ):
    """Combine the signal volume with its timecourse

    Apply the convolution of the HRF and stimulus time course to the
    volume.

    Parameters
    ----------

    signal_function : timepoint by timecourse array, float
        The timecourse of the signal over time. If there is only one column
        then the same timecourse is applied to all non-zero voxels in
        volume_signal. If there is more than one column then each column is
        paired with a non-zero voxel in the volume_signal (a 3d numpy array
        generated in generate_signal).

    volume_signal : multi dimensional array, float
        The volume containing the signal to be convolved with the same
        dimensions as the output volume. The elements in volume_signal
        indicate how strong each signal in signal_function are modulated by
        in the output volume


    Returns
    ----------
    signal : multidimensional array, float
        The convolved signal volume with the same 3d as volume signal and
        the same 4th dimension as signal_function

    """

    # How many timecourses are there within the signal_function
    timepoints = signal_function.shape[0]
    timecourses = signal_function.shape[1]

    # Preset volume
    signal = np.zeros([volume_signal.shape[0], volume_signal.shape[
        1], volume_signal.shape[2], timepoints])

    # Find all the non-zero voxels in the brain
    idxs = np.where(volume_signal != 0)
    if timecourses == 1:
        # If there is only one time course supplied then duplicate it for
        # every voxel
        signal_function = np.matlib.repmat(signal_function, 1, len(idxs[0]))

    elif len(idxs[0]) != timecourses:
        raise IndexError('The number of non-zero voxels in the volume and '
                         'the number of timecourses does not match. Aborting')

    # For each coordinate with a non zero voxel, fill in the timecourse for
    # that voxel
    for idx_counter in range(len(idxs[0])):
        x = idxs[0][idx_counter]
        y = idxs[1][idx_counter]
        z = idxs[2][idx_counter]

        # Pull out the function for this voxel
        signal_function_temp = signal_function[:, idx_counter]

        # Multiply the voxel value by the function timecourse
        signal[x, y, z, :] = volume_signal[x, y, z] * signal_function_temp

    return signal



