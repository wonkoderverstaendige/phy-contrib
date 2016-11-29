# -*- coding: utf-8 -*-

"""Template GUI."""


#------------------------------------------------------------------------------
# Imports
#------------------------------------------------------------------------------

import inspect
import logging
from operator import itemgetter
import os.path as op

import click
import numpy as np

from phy.cluster.supervisor import Supervisor
from phy.cluster.views import (WaveformView,
                               FeatureView,
                               TraceView,
                               CorrelogramView,
                               ScatterView,
                               select_traces,
                               )
from phy.gui import create_app, run_app, GUI
from phy.io.array import (Selector,
                          )
from phy.io.context import Context
from phy.stats import correlograms
from phy.utils import Bunch, IPlugin, EventEmitter
from phy.utils._color import ColorSelector, _colormap
from phy.utils._misc import _read_python
from phy.utils.cli import _run_cmd, _add_log_file

from .model import TemplateModel

logger = logging.getLogger(__name__)


#------------------------------------------------------------------------------
# Utils
#------------------------------------------------------------------------------

def _dat_n_samples(filename, dtype=None, n_channels=None, offset=None):
    assert dtype is not None
    item_size = np.dtype(dtype).itemsize
    offset = offset if offset else 0
    n_samples = (op.getsize(filename) - offset) // (item_size * n_channels)
    assert n_samples >= 0
    return n_samples


def _dat_to_traces(dat_path, n_channels=None, dtype=None, offset=None):
    assert dtype is not None
    assert n_channels is not None
    n_samples = _dat_n_samples(dat_path,
                               n_channels=n_channels,
                               dtype=dtype,
                               offset=offset,
                               )
    return np.memmap(dat_path, dtype=dtype, shape=(n_samples, n_channels),
                     offset=offset)


#------------------------------------------------------------------------------
# Template views
#------------------------------------------------------------------------------

def subtract_templates(traces,
                       start=None,
                       spike_times=None,
                       spike_clusters=None,
                       amplitudes=None,
                       spike_templates=None,
                       sample_rate=None,
                       ):
    traces = traces.copy()
    st = spike_times
    w = spike_templates
    n_spikes, n_samples_t, n_channels = w.shape
    n = traces.shape[0]
    for index in range(w.shape[0]):
        t = int(round((st[index] - start) * sample_rate))
        i, j = n_samples_t // 2, n_samples_t // 2 + (n_samples_t % 2)
        assert i + j == n_samples_t
        x = w[index] * amplitudes[index]  # (n_samples, n_channels)
        sa, sb = t - i, t + j
        if sa < 0:
            x = x[-sa:, :]
            sa = 0
        elif sb > n:
            x = x[:-(sb - n), :]
            sb = n
        traces[sa:sb, :] -= x
    return traces


#------------------------------------------------------------------------------
# Template Controller
#------------------------------------------------------------------------------

class TemplateFeatureView(ScatterView):
    def on_select(self, cluster_ids=None):
        super(ScatterView, self).on_select(cluster_ids)
        cluster_ids = self.cluster_ids
        n_clusters = len(cluster_ids)
        if n_clusters != 2:
            return
        d = self.coords(cluster_ids)

        # Plot the points.
        with self.building():
            for i, cluster_id in enumerate(cluster_ids):
                x = d.get('x%d' % i)
                y = d.get('y%d' % i)
                data_bounds = d.get('data_bounds', 'auto')
                assert x.ndim == y.ndim == 1
                assert x.shape == y.shape

                self.scatter(x=x, y=y,
                             color=tuple(_colormap(i)) + (.5,),
                             size=self._default_marker_size,
                             data_bounds=data_bounds,
                             )


class TemplateController(EventEmitter):
    gui_name = 'TemplateGUI'

    n_spikes_waveforms = 100
    batch_size_waveforms = 10

    n_spikes_features = 10000
    n_spikes_amplitudes = 10000

    def __init__(self, dat_path, config_dir=None, **kwargs):
        super(TemplateController, self).__init__()
        dat_path = op.realpath(dat_path)
        self.model = TemplateModel(dat_path, **kwargs)
        self.cache_dir = op.join(self.model.dir_path, '.phy')
        self.context = Context(self.cache_dir)
        self.config_dir = config_dir

        self.supervisor = self._set_supervisor()
        self.selector = self._set_selector()
        self.color_selector = ColorSelector()

    # Internal methods
    # -------------------------------------------------------------------------

    def _set_supervisor(self):
        # Load the new cluster id.
        new_cluster_id = self.context.load('new_cluster_id'). \
            get('new_cluster_id', None)
        cluster_groups = self.model.metadata['group']
        supervisor = Supervisor(self.model.spike_clusters,
                                similarity=self.similarity,
                                cluster_groups=cluster_groups,
                                new_cluster_id=new_cluster_id,
                                )
        supervisor.add_column(self.get_best_channel, name='channel')
        supervisor.add_column(self.get_probe_depth, name='depth')
        return supervisor

    def _set_selector(self):
        def spikes_per_cluster(cluster_id):
            return self.supervisor.clustering.spikes_per_cluster[cluster_id]
        return Selector(spikes_per_cluster)

    def _add_view(self, gui, view, name=None):
        if 'name' in inspect.getargspec(view.attach).args:
            view.attach(gui, name=name)
        else:
            view.attach(gui)
        self.emit('add_view', gui, view)
        return view

    # Model methods
    # -------------------------------------------------------------------------

    def get_template_counts(self, cluster_id):
        """Return a histogram of the number of spikes in each template for
        a given cluster."""
        spike_ids = self.supervisor.clustering.spikes_per_cluster[cluster_id]
        st = self.model.spike_templates[spike_ids]
        return np.bincount(st, minlength=self.model.n_templates)

    def get_template_for_cluster(self, cluster_id):
        """Return the template associated to each cluster."""
        spike_ids = self.supervisor.clustering.spikes_per_cluster[cluster_id]
        st = self.model.spike_templates[spike_ids]
        template_ids, counts = np.unique(st, return_counts=True)
        ind = np.argmax(counts)
        return template_ids[ind]

    def similarity(self, cluster_id):
        """Return the list of similar clusters to a given cluster."""
        # Templates of the cluster.
        temp_i = np.nonzero(self.get_template_counts(cluster_id))[0]
        # The similarity of the cluster with each template.
        sims = np.max(self.model.similar_templates[temp_i, :], axis=0)

        def _sim_ij(cj):
            # Templates of the cluster.
            if cj < self.model.n_templates:
                return sims[cj]
            temp_j = np.nonzero(self.get_template_counts(cj))[0]
            return np.max(sims[temp_j])

        out = [(cj, _sim_ij(cj))
               for cj in self.supervisor.clustering.cluster_ids]
        return sorted(out, key=itemgetter(1), reverse=True)

    def get_best_channel(self, cluster_id):
        """Return the best channel of a given cluster."""
        template_id = self.get_template_for_cluster(cluster_id)
        return self.model.get_template(template_id).best_channel

    def get_best_channels(self, cluster_id):
        """Return the best channels of a given cluster."""
        template_id = self.get_template_for_cluster(cluster_id)
        return self.model.get_template(template_id).channels

    def get_probe_depth(self, cluster_id):
        """Return the depth of a cluster."""
        channel_id = self.get_best_channel(cluster_id)
        return self.model.channel_positions[channel_id][1]

    # Waveforms
    # -------------------------------------------------------------------------

    def _get_waveforms(self, cluster_id):
        spike_ids = self.selector.select_spikes([cluster_id],
                                                self.n_spikes_waveforms,
                                                self.batch_size_waveforms,
                                                )
        channel_ids = self.get_best_channels(cluster_id)
        data = self.model.get_waveforms(spike_ids, channel_ids)
        return Bunch(data=data,
                     spike_ids=spike_ids,
                     channel_ids=channel_ids,
                     )

    def add_waveform_view(self, gui):
        v = WaveformView(waveforms=self._get_waveforms,
                         channel_positions=self.model.channel_positions,
                         channel_order=self.model.channel_mapping,
                         best_channels=self.get_best_channels,
                         )
        return self._add_view(gui, v)

    # Features
    # -------------------------------------------------------------------------

    def _get_spike_ids(self, cluster_id=None):
        nsf = self.n_spikes_features
        if cluster_id is None:
            # Background points.
            ns = self.model.n_spikes
            return np.arange(0, ns, max(1, ns // nsf))
        else:
            return self.selector.select_spikes([cluster_id], nsf)

    def _get_spike_times(self, cluster_id=None):
        spike_ids = self._get_spike_ids(cluster_id)
        return Bunch(data=self.model.spike_times[spike_ids],
                     lim=(0., self.model.duration))

    def _get_features(self, cluster_id=None, channel_ids=None):
        spike_ids = self._get_spike_ids(cluster_id)
        if cluster_id is not None:
            channel_ids = self.get_best_channels(cluster_id)
        assert channel_ids is not None
        data = self.model.get_features(spike_ids, channel_ids)
        return Bunch(data=data,
                     channel_ids=channel_ids,
                     )

    def add_feature_view(self, gui):
        v = FeatureView(features=self._get_features,
                        attributes={'time': self._get_spike_times}
                        )
        return self._add_view(gui, v)

    # Template  features
    # -------------------------------------------------------------------------

    def _get_template_features(self, cluster_ids):
        assert len(cluster_ids) == 2
        clu0, clu1 = cluster_ids

        s0 = self._get_spike_ids(clu0)
        s1 = self._get_spike_ids(clu1)

        n0 = self.get_template_counts(clu0)
        n1 = self.get_template_counts(clu1)

        t0 = self.model.get_template_features(s0)
        t1 = self.model.get_template_features(s1)

        x0 = np.average(t0, weights=n0, axis=1)
        y0 = np.average(t0, weights=n1, axis=1)

        x1 = np.average(t1, weights=n0, axis=1)
        y1 = np.average(t1, weights=n1, axis=1)

        return Bunch(x0=x0, y0=y0, x1=x1, y1=y1,
                     data_bounds=(min(x0.min(), x1.min()),
                                  min(y0.min(), y1.min()),
                                  max(y0.max(), y1.max()),
                                  max(y0.max(), y1.max()),
                                  ),
                     )

    def add_template_feature_view(self, gui):
        v = TemplateFeatureView(coords=self._get_template_features,
                                )
        return self._add_view(gui, v, name='TemplateFeatureView')

    # Traces
    # -------------------------------------------------------------------------

    def _get_traces(self, interval):
        m = self.model
        p = self.supervisor
        cs = self.color_selector
        sr = m.sample_rate
        traces = select_traces(m.traces, interval, sample_rate=sr)
        out = Bunch(data=traces)
        a, b = m.spike_times.searchsorted(interval)
        s0, s1 = int(round(interval[0] * sr)), int(round(interval[1] * sr))
        out.waveforms = []
        k = m.n_samples_templates // 2
        for i in range(a, b):
            t = m.spike_times[i]
            c = m.spike_clusters[i]
            cg = p.cluster_meta.get('group', c)
            channel_ids = self.get_best_channels(c)
            s = int(round(t * sr)) - s0
            # Skip partial spikes.
            if s - k < 0 or s + k >= (s1 - s0):
                continue
            color = cs.get(c, cluster_ids=p.selected, cluster_group=cg),
            d = Bunch(data=traces[s - k:s + k, channel_ids],
                      channel_ids=channel_ids,
                      start_time=(s + s0 - k) / sr,
                      cluster_id=c,
                      color=color,
                      )
            out.waveforms.append(d)
        return out

    def add_trace_view(self, gui):
        m = self.model
        v = TraceView(traces=self._get_traces,
                      n_channels=m.n_channels,
                      sample_rate=m.sample_rate,
                      duration=m.duration,
                      )
        return self._add_view(gui, v)

    # Correlograms
    # -------------------------------------------------------------------------

    def _get_correlograms(self, cluster_ids, bin_size, window_size):
        spike_ids = self.selector.select_spikes(cluster_ids, 100000)
        st = self.model.spike_times[spike_ids]
        sc = self.supervisor.clustering.spike_clusters[spike_ids]
        return correlograms(st,
                            sc,
                            sample_rate=self.model.sample_rate,
                            cluster_ids=cluster_ids,
                            bin_size=bin_size,
                            window_size=window_size,
                            )

    def add_correlogram_view(self, gui):
        m = self.model
        v = CorrelogramView(correlograms=self._get_correlograms,
                            sample_rate=m.sample_rate,
                            )
        return self._add_view(gui, v)

    # Amplitudes
    # -------------------------------------------------------------------------

    def _get_amplitudes(self, cluster_id):
        n = self.n_spikes_amplitudes
        spike_ids = self.selector.select_spikes([cluster_id], n)
        x = self.model.spike_times[spike_ids]
        y = self.model.amplitudes[spike_ids]
        return Bunch(x=x, y=y)

    def add_amplitude_view(self, gui):
        v = ScatterView(coords=self._get_amplitudes,
                        )
        return self._add_view(gui, v, name='AmplitudeView')

    # GUI
    # -------------------------------------------------------------------------

    def create_gui(self, **kwargs):
        gui = GUI(name=self.gui_name,
                  subtitle=self.model.dat_path,
                  config_dir=self.config_dir,
                  **kwargs)

        self.supervisor.attach(gui)

        self.add_waveform_view(gui)
        self.add_trace_view(gui)
        self.add_feature_view(gui)
        self.add_template_feature_view(gui)
        self.add_correlogram_view(gui)
        self.add_amplitude_view(gui)

        return gui


#------------------------------------------------------------------------------
# Template GUI plugin
#------------------------------------------------------------------------------

def _run(params):
    controller = TemplateController(**params)
    gui = controller.create_gui()
    gui.show()
    run_app()
    gui.close()
    del gui


class TemplateGUIPlugin(IPlugin):
    """Create the `phy template-gui` command for Kwik files."""

    def attach_to_cli(self, cli):

        # Create the `phy cluster-manual file.kwik` command.
        @cli.command('template-gui')
        @click.argument('params-path', type=click.Path(exists=True))
        @click.pass_context
        def gui(ctx, params_path):
            """Launch the Template GUI on a params.py file."""

            # Create a `phy.log` log file with DEBUG level.
            _add_log_file(op.join(op.dirname(params_path), 'phy.log'))

            create_app()

            params = _read_python(params_path)
            params['dtype'] = np.dtype(params['dtype'])

            _run_cmd('_run(params)', ctx, globals(), locals())

        @cli.command('template-describe')
        @click.argument('params-path', type=click.Path(exists=True))
        def describe(params_path):
            """Describe a template dataset."""
            params = _read_python(params_path)
            TemplateModel(**params).describe()
