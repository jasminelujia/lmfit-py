from copy import deepcopy
from itertools import chain

from .model import Model
from .models import ExponentialModel  # arbitrary default for menu

from .asteval import Interpreter
from .astutils import NameFinder
from .minimizer import check_ast_errors


try:
    import IPython
except ImportError:
    has_ipython = False
else:
    has_ipython = True
    from IPython.html import widgets
    from IPython.display import HTML, display, clear_output
    from IPython.utils.traitlets import link

def build_param_widget(p):
    if p.value is not None:
        param_widget = widgets.FloatText(description=p.name, value=p.value, min=p.min, max=p.max)
    else:
        param_widget = widgets.FloatText(description=p.name, min=p.min, max=p.max)
    return param_widget

class Fitter(object):
    """This an interactive container for fitting models to particular data.

    It maintains the attributes `current_params` and `current_result`. When
    its fit() method is called, the best fit becomes the new `current_params`.
    The most basic usage is iteratively fitting data, taking advantage of
    this stateful memory that keep the parameters between each fit.

    If matplotlib as available, a plot is generated by fit(), showing the
    data, the initial guess, and the best fit.

    If IPython is available, it uses the IPython notebook's rich display
    to fit data interactively in a web-based GUI. The Parameters are
    represented in a web-based form that is kept in sync with `current_params`. 
    All subclasses to Model, including user-defined ones, are shown in a
    drop-down menu.

    Clicking the "Fit" button updates a plot, as above, and updates the
    Parameters in the form to reflect the best fit.
    
    Examples
    --------
    >>> fitter = Fitter(data, model=SomeModel, x=x)
    # In the IPython notebook, this displays a form with text
    # fields for each Parameter in the model and a "Fit" button.

    >>> fitter.model
    # This property can be changed, to try different models on the same
    # data with the same independent vars. 
    # (This is especially handy in the notebook.)
    
    >>> fitter.current_params
    # This copy of the model's Parameters is updated after each fit.
    # In the IPython notebook ,it is always in sync with
    # the current values entered into the text fields.
    
    >>> fitter.fit()
    # Perform a fit using fitter.current_params as a guess.
    # Optionally, pass a params argument or individual keyword arguments
    # to override current_params. This method can also be called by clicking
    # the "Fit" button in the web form.
    
    >>> fitter.current_result
    # This is the result of the latest fit. It contain the usual
    # copies of the Parameters, in the attributes params and init_params.
    """
    def __init__(self, data, model=None, **kwargs):
        self._data = data
        self.kwargs = kwargs
        if has_ipython:
            # Dropdown menu of all subclasses of Model, incl. user-defined.
            self.models_menu = widgets.Dropdown()
            all_models = {m.__name__: m for m in Model.__subclasses__()}
            self.models_menu.values = all_models
            self.models_menu.on_trait_change(self._on_model_value_change, 'value')
            # Button to trigger fitting.
            self.fit_button = widgets.Button(description='Fit')
            self.fit_button.on_click(self._on_fit_button_click)

            # Button to trigger guessing.
            self.guess_button = widgets.Button(description='Auto-Guess')
            self.guess_button.on_click(self._on_guess_button_click)

            # Parameter widgets are (re-)built when the model is (re-)set.
        if model is None:
            model = ExponentialModel
        self.model = model

    def _on_model_value_change(self, name, value):
        self.model = value

    def _on_fit_button_click(self, b):
        self.fit()

    def _on_guess_button_click(self, b):
        self.guess()

    @property
    def data(self):
        return self._data

    @data.setter
    def data(self, value):
        self._data = value 
        
    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, value):
        first_run = not hasattr(self, 'param_widgets')
        if not first_run:
            # Remove all Parameter widgets, and replace them with widgets
            # for the new model.
            for pw in self.param_widgets:
                pw.close()
        if callable(value):
            model = value()
        else:
            model = value
        self._model = model
        self.current_result = None
        self._current_params = model.make_params()

        # Use these to evaluate any Parameters that use expressions.
        self.asteval = Interpreter()
        self.namefinder = NameFinder()

        if has_ipython:
            self.models_menu.value = value 
            self.param_widgets = [build_param_widget(p)
                                  for _, p in self._current_params.items()]
            if not first_run:
                for pw in self.param_widgets:
                    display(pw)

        self.guess()

    @property
    def current_params(self):
        """Each time fit() is called, these will be updated to reflect
        the latest best params. They will be used as the initial guess
        for the next fit, unless overridden by arguments to fit()."""
        if has_ipython:
            # Sync current_params with widget.
            for pw in self.param_widgets:
                self._current_params[pw.description].value = pw.value
        return self._current_params
            
    @current_params.setter
    def current_params(self, value):
        self._current_params = value
        if has_ipython:
            for pw in self.param_widgets:
                pw.value = self._current_params[pw.description].value

    def guess(self):
        count_indep_vars = len(self.model.independent_vars)
        guessing_disabled = False 
        try:
            if count_indep_vars == 0:
                guess = self.model.guess(self._data)
            elif count_indep_vars == 1:
                key = self.model.independent_vars[0]
                val = self.kwargs[key]
                d = {key: val}
                guess = self.model.guess(self._data, **d)
        except NotImplementedError:
            guessing_disabled = True 
        self.guess_button.disabled = guessing_disabled

        # Compute values for expression-based Parameters.
        self.__assign_deps(guess)
        for _, p in guess.items():
            if p.value is None:
                self.__update_paramval(guess, p.name)

        self.current_params = guess

    def __assign_deps(self, params):
        # N.B. This does not use self.current_params but rather
        # new Parameters that are being built by self.guess().
        for name, par in params.items():
            if par.expr is not None:
                par.ast = self.asteval.parse(par.expr)
                check_ast_errors(self.asteval.error)
                par.deps = []
                self.namefinder.names = []
                self.namefinder.generic_visit(par.ast)
                for symname in self.namefinder.names:
                    if (symname in self.current_params and
                        symname not in par.deps):
                        par.deps.append(symname)
                self.asteval.symtable[name] = par.value
                if par.name is None:
                    par.name = name

    def __update_paramval(self, params, name):
        # N.B. This does not use self.current_params but rather
        # new Parameters that are being built by self.guess().
        par = params[name]
        if getattr(par, 'expr', None) is not None:
            if getattr(par, 'ast', None) is None:
                par.ast = self.asteval.parse(par.expr)
            if par.deps is not None:
                for dep in par.deps:
                    self.__update_paramval(params, dep)
            par.value = self.asteval.run(par.ast)
            out = check_ast_errors(self.asteval.error)
            if out is not None:
                self.asteval.raise_exception(None)
        self.asteval.symtable[name] = par.value

    def fit(self, *args, **kwargs):
        "Use current_params unless overridden by arguments passed here."
        guess = dict(self.current_params)
        guess.update(self.kwargs)  # from __init__, e.g. x=x
        guess.update(kwargs)
        self.current_result = self.model.fit(self._data, *args, **guess) 
        self.current_params = self.current_result.params
        self.plot()
        
    def plot(self):
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            pass
        if has_ipython:
            clear_output(wait=True)
        fig, ax = plt.subplots()
        count_indep_vars = len(self.model.independent_vars)
        if count_indep_vars == 0:
            ax.plot(self._data)
        elif count_indep_vars == 1:
            indep_var = self.kwargs[self.model.independent_vars[0]]
            ax.plot(indep_var, self._data, marker='o', linestyle='none')
        else:
            raise NotImplementedError("Cannot plot models with more than one "
                                      "indepedent variable.")
        result = self.current_result  # alias for brevity
        if not result:
            return  # short-circuit the rest of the plotting
        if count_indep_vars == 0:
            ax.plot(result.init_fit, color='gray')
            ax.plot(result.best_fit, color='red')
        elif count_indep_vars == 1:
            ax.plot(indep_var, result.init_fit, color='gray')
            ax.plot(indep_var, result.best_fit, color='red')
            
    def _repr_html_(self):
        display(self.models_menu)
        display(self.fit_button)
        display(self.guess_button)
        for pw in self.param_widgets:
            display(pw)
        self.plot()
