# Force MKL (CPU BLAS) to use one core, faster
import os
os.environ["OMP_NUM_THREADS"] = '1'

import torch
from torch import nn
import torch.nn.functional as F
import argparse
import yaml
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
import numpy as np
from glob import glob

import logging
import logging.config

# Force MKL (CPU BLAS) to use one core, faster
os.environ["OMP_NUM_THREADS"] = '1'

# Import problems and problem factory.
import sys
sys.path.append(os.path.join(os.path.dirname(__file__), 'problems'))
sys.path.append(os.path.join(os.path.dirname(__file__), 'models'))
from problems.problem_factory import ProblemFactory
from models.model_factory import ModelFactory

from misc.app_state import AppState

logging.getLogger('matplotlib').setLevel(logging.WARNING)


def show_sample(prediction, target, mask, sample_number=0):
    """ Shows the sample (both input and target sequences) using matplotlib."""
    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True)
    # Set ticks.
    ax1.xaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    ax1.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))
    ax2.yaxis.set_major_locator(ticker.MaxNLocator(integer=True))

    # Set labels.
    ax1.set_title('Prediction')
    ax1.set_ylabel('Data bits')
    ax2.set_title('Target')
    ax2.set_ylabel('Data bits')
    # ax2.set_ylabel('Data bits')
    ax2.set_xlabel('Item number')

    # Set data.
    ax1.imshow(np.transpose((prediction[sample_number, :, :]).detach().numpy(), [1, 0]))
    ax2.imshow(np.transpose((target[sample_number, :, :]).detach().numpy(), [1, 0]))

    plt.show()

    # Plot!


if __name__ == '__main__':
    # Create parser with list of  runtime arguments.
    parser = argparse.ArgumentParser()
    parser.add_argument('-i', type=str, default='', dest='input_dir',
                        help='Input path, containing the saved parameters as well as the yaml file')
    parser.add_argument('-v', action='store_true', dest='visualize',
                        help='Activate dynamic visualization')
    parser.add_argument('--log', action='store', dest='log', type=str, default='info',
                        choices=['critical', 'error', 'warning', 'info', 'debug', 'notset'],
                        help="Log level. Default is INFO.")
    parser.add_argument('-e', action='store', dest='episode', type=int,
                        help="Episode of model. Default is 0.")

    # Parse arguments.
    FLAGS, unparsed = parser.parse_known_args()

    # Check if input directory was selected.
    if FLAGS.input_dir == '':
        print('Please pass input path folder as -i parameter')
        exit(-1)

    # Check if file exists.
    if not os.path.isdir(FLAGS.input_dir):
        print('Input path {} does not exist'.format(FLAGS.input_dir))
        exit(-2)
        
    # Logging
    log_file = FLAGS.input_dir + '/msgs_test.log'
    def logfile():
        return logging.FileHandler(log_file)

    with open('logger_config.yaml', 'rt') as f:
        config = yaml.load(f.read())
        logging.config.dictConfig(config)

    logger = logging.getLogger('Test')
    logger.setLevel(getattr(logging, FLAGS.log.upper(), None))

    # Initialize the application state singleton.
    app_state = AppState()
    if FLAGS.visualize:
        app_state.visualize = True

    # Read YAML file
    with open(FLAGS.input_dir + "/train_settings.yaml", 'r') as stream:
        config_loaded = yaml.load(stream)

    # set seed
    if config_loaded["settings"]["seed_torch"] != -1:
        torch.manual_seed(config_loaded["settings"]["seed_torch"])
        torch.cuda.manual_seed_all(config_loaded["settings"]["seed_torch"])

    if config_loaded["settings"]["seed_numpy"] != -1:
        np.random.seed(config_loaded["settings"]["seed_numpy"])

    # Create output file
    test_file = open(FLAGS.input_dir + '/test.csv', 'w', 1)
    test_file.write('episode, accuracy, loss, length\n')

    # Build new problem
    problem = ProblemFactory.build_problem(config_loaded['problem_test'])

    # Build model
    model = ModelFactory.build_model(config_loaded['model'])

    criterion = nn.BCEWithLogitsLoss()

    if FLAGS.episode != None:
        # load the trained model
        model_file_name = FLAGS.input_dir + '/models/model_parameters_episode_{:05d}'.format(FLAGS.episode)
    else:
        model_file_name = glob(FLAGS.input_dir + '/models/model_parameters_episode_*')[-1]

    model.load_state_dict(
        torch.load(model_file_name,
                   map_location=lambda storage, loc: storage)  # This is to be able to load CUDA-trained model on CPU
    )

    for episode, (inputs, unmasked_target, mask) in enumerate(problem.return_generator()):

        # apply the trained model
        unmasked_output = model(inputs)

        if config_loaded['settings']['use_mask']:
            output = unmasked_output[:, mask[0], :]
            target = unmasked_target[:, mask[0], :]
        else:
            output = unmasked_output
            target = unmasked_target

        loss = criterion(output, target)

        output = F.sigmoid(output)

        # test accuracy
        output = torch.round(output)
        acc = 1 - torch.abs(output-target)
        accuracy = acc.mean()
        format_str = 'episode {:05d}; acc={:12.10f}; loss={:12.10f}; length={:d} [Test]'
        logger.info(format_str.format(episode, accuracy, loss, unmasked_target.size(1)))
        # plot data
        # show_sample(output, targets, mask)

        format_str = '{:05d}, {:12.10f}, {:12.10f}, {:03d}'
        format_str = format_str + '\n'
        test_file.write(format_str.format(episode, accuracy, loss, unmasked_target.size(1)))

        if app_state.visualize:
            mask_not = (mask == 0)
            unmasked_output[0][mask_not[0], :] = 0

            is_closed = model.plot_sequence(inputs[0].detach().numpy(), unmasked_output[0].detach().numpy(), unmasked_target[0].detach().numpy())
            if is_closed:
                break
        else:
            break
