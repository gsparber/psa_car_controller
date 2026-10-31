import logging
from logging import DEBUG

from dash import dcc
from dash.dependencies import Output, Input, State

logger = logging.getLogger(__name__)
PARAMS_STORE = 'clientside-params-store'


class Graph:
    def __init__(self, graph_id, x, y: [], figure):
        self.graph_id = graph_id
        self.x = x
        self.y = y
        self.figure = figure


class Table:
    def __init__(self, table_id, src, figure):
        self.table_id = table_id
        self.src = src
        self.figure = figure
        self.date_columns = []


def figures_to_dict(figures):
    el_list = []
    for figure in figures:
        res = {}
        for key, value in figure.__dict__.items():
            if key != "figure":
                res[key] = value
        el_list.append(res)
    return el_list


def map_view_store(map_id):
    return f"{map_id}-view-store"


class FigureFilter:

    def __init__(self):
        self.graphs = []
        self.tables = []
        self.maps = []
        self.src = {}

    def add_map(self, dash_Graph, latitude, longitude, figure):
        self.maps.append(Graph(dash_Graph.id, latitude, longitude, figure))
        return dash_Graph

    def add_graph(self, dash_Graph, x, y, figure):
        self.graphs.append(Graph(dash_Graph.id, x, y, figure))
        return dash_Graph

    def add_table(self, src, figure):
        try:
            table = Table(figure.id, src, figure)
            table.date_columns = [col["id"][:-4] for col in figure.columns if col["type"] == "datetime" and
                                  col["id"].endswith("_str")]
            self.tables.append(table)
        except AttributeError:
            logger.debug("figure isn't a table")

    def __get_table_date_column_id(self):
        res = {table.src: table.date_columns for table in self.tables}
        return res

    def __get_figures(self):
        return {"graph": [graph.figure for graph in self.graphs],
                "tables": [table.figure for table in self.tables],
                "maps": [map.figure for map in self.maps]}

    @staticmethod
    def __get_graph_x_label(graphs):
        return [graph.x for graph in graphs]

    @staticmethod
    def __get_graph_y_label(graphs):
        return [graph.y for graph in graphs]

    def get_params(self, config: dict):
        return {
            "date_columns": self.__get_table_date_column_id(),
            "table_src": figures_to_dict(self.tables),
            "graph_x_label": self.__get_graph_x_label(self.graphs),
            "graph_y_label": self.__get_graph_y_label(self.graphs),
            "map_x_label": self.__get_graph_x_label(self.maps),
            "map_y_label": self.__get_graph_y_label(self.maps),
            "log_level": 10 if logger.isEnabledFor(DEBUG) else 20,
            "config": config
        }

    def get_store(self, config: dict):
        return [dcc.Store(id='clientside-figure-store', data=self.__get_figures()),
                dcc.Store(id='clientside-data-store', data=self.src),
                dcc.Store(id=PARAMS_STORE, data=self.get_params(config)),
                *[dcc.Store(id=map_view_store(graph.graph_id)) for graph in self.maps]]


def set_clientside_callback(dash_app, table_ids: list, figure_ids: list, map_ids: list):
    """Call it at import, once. The browser asks for the callbacks when it loads the app, a callback registered while
    a page is built reaches it only after a reload. So it knows just the ids, the rest comes with the page in a store.
    table_ids and figure_ids have the order of the filter that fills the store: tables, graphs, then maps.
    map_ids are the maps among the figures: the view the user gave them stays when the figure changes."""
    sort_args = [f"sort{i}" for i in range(len(table_ids))]
    sort_dict = ", ".join(f'"{table_id}": {arg}' for table_id, arg in zip(table_ids, sort_args))
    view_args = [f"view{i}" for i in range(len(map_ids))]
    dash_app.clientside_callback(
        f"""function(data, range, figures, {", ".join(sort_args + view_args)}, params) {{
                return filterAndSort(data, range, figures, params, params.log_level, {{{sort_dict}}}, params.config,
                                     [{", ".join(view_args)}])
            }}""",
        *[Output(table_id, "data") for table_id in table_ids],
        *[Output(figure_id, "figure") for figure_id in figure_ids],
        Input('clientside-data-store', 'data'),
        Input('date-slider', 'value'),
        Input('clientside-figure-store', 'data'),
        *[Input(table_id, 'sort_by') for table_id in table_ids],
        *[State(map_view_store(map_id), 'data') for map_id in map_ids],
        State(PARAMS_STORE, 'data'))
    for map_id in map_ids:
        dash_app.clientside_callback("function(relayoutData) { return userMapView(relayoutData) }",
                                     Output(map_view_store(map_id), 'data'),
                                     Input(map_id, 'relayoutData'),
                                     prevent_initial_call=True)
    for table_id in table_ids:
        dash_app.clientside_callback("function(_) { return tableLocale() }",
                                     Output(table_id, 'locale_format'),
                                     Input('clientside-data-store', 'data'))
        dash_app.clientside_callback(
            f"function(sortBy) {{ return skipUnsorted('{table_id}', sortBy) }}",
            Output(table_id, 'sort_by'),
            Input(table_id, 'sort_by'))
