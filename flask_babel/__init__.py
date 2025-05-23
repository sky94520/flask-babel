"""
    flask_babel
    ~~~~~~~~~~~

    Implements i18n/l10n support for Flask applications based on Babel.

    :copyright: (c) 2013 by Armin Ronacher, Daniel Neuhäuser.
    :license: BSD, see LICENSE for more details.
"""

import os
from gettext import GNUTranslations
from dataclasses import dataclass
from importlib import resources
from types import SimpleNamespace
from datetime import datetime
from contextlib import contextmanager
from typing import List, Callable, Optional, Union

from babel.support import Translations, NullTranslations
from flask import current_app, g
from babel import dates, numbers, support, Locale
from pytz import timezone, UTC
from werkzeug.datastructures import ImmutableDict
from werkzeug.utils import cached_property

from flask_babel.speaklater import LazyString


@dataclass
class BabelConfiguration:
    """Application-specific configuration for Babel."""

    default_locale: str
    default_timezone: str
    default_domain: str
    default_directories: List[str]
    translation_directories: List[str]
    plugin_translation_packages: List[str]

    instance: "Babel"

    locale_selector: Optional[Callable] = None
    timezone_selector: Optional[Callable] = None


def get_babel(app=None) -> "BabelConfiguration":
    app = app or current_app
    if not hasattr(app, "extensions"):
        app.extensions = {}
    return app.extensions["babel"]


class Babel:
    """Central controller class that can be used to configure how
    Flask-Babel behaves.  Each application that wants to use Flask-Babel
    has to create, or run :meth:`init_app` on, an instance of this class
    after the configuration was initialized.
    """

    default_date_formats = ImmutableDict(
        {
            "time": "medium",
            "date": "medium",
            "datetime": "medium",
            "time.short": None,
            "time.medium": None,
            "time.full": None,
            "time.long": None,
            "date.short": None,
            "date.medium": None,
            "date.full": None,
            "date.long": None,
            "datetime.short": None,
            "datetime.medium": None,
            "datetime.full": None,
            "datetime.long": None,
        }
    )

    def __init__(
        self, app=None, date_formats=None, configure_jinja=True, *args, **kwargs
    ):
        """Creates a new Babel instance.

        If an application is passed, it will be configured with the provided
        arguments. Otherwise, :meth:`init_app` can be used to configure the
        application later.
        """
        self._configure_jinja = configure_jinja
        self.date_formats = date_formats

        if app is not None:
            self.init_app(app, *args, **kwargs)

    def init_app(
        self,
        app,
        default_locale="en",
        default_domain="messages",
        default_translation_directories="translations",
        default_timezone="UTC",
        locale_selector=None,
        timezone_selector=None,
    ):
        """
        Initializes the Babel instance for use with this specific application.

        :param app: The application to configure
        :param default_locale: The default locale to use for this application
        :param default_domain: The default domain to use for this application
        :param default_translation_directories: The default translation
                                                directories to use for this
                                                application
        :param default_timezone: The default timezone to use for this
                                 application
        :param locale_selector: The function to use to select the locale
                                for a request
        :param timezone_selector: The function to use to select the
                                  timezone for a request
        """
        if not hasattr(app, "extensions"):
            app.extensions = {}

        directories = app.config.get(
            "BABEL_TRANSLATION_DIRECTORIES", default_translation_directories
        ).split(";")

        app.extensions["babel"] = BabelConfiguration(
            default_locale=app.config.get("BABEL_DEFAULT_LOCALE", default_locale),
            default_timezone=app.config.get("BABEL_DEFAULT_TIMEZONE", default_timezone),
            default_domain=app.config.get("BABEL_DOMAIN", default_domain),
            default_directories=directories,
            translation_directories=list(self._resolve_directories(directories, app)),
            plugin_translation_packages=list(),
            instance=self,
            locale_selector=locale_selector,
            timezone_selector=timezone_selector,
        )

        # a mapping of Babel datetime format strings that can be modified
        # to change the defaults.  If you invoke :func:`format_datetime`
        # and do not provide any format string Flask-Babel will do the
        # following things:
        #
        # 1.   look up ``date_formats['datetime']``.  By default, ``'medium'``
        #      is returned to enforce medium length datetime formats.
        # 2.   ``date_formats['datetime.medium'] (if ``'medium'`` was
        #      returned in step one) is looked up.  If the return value
        #      is anything but `None` this is used as new format string.
        #      otherwise the default for that language is used.
        if self.date_formats is None:
            self.date_formats = self.default_date_formats.copy()

        if self._configure_jinja:
            app.jinja_env.filters.update(
                datetimeformat=format_datetime,
                dateformat=format_date,
                timeformat=format_time,
                timedeltaformat=format_timedelta,
                numberformat=format_number,
                decimalformat=format_decimal,
                currencyformat=format_currency,
                percentformat=format_percent,
                scientificformat=format_scientific,
            )
            app.jinja_env.add_extension("jinja2.ext.i18n")
            app.jinja_env.install_gettext_callables(
                gettext=lambda s: get_translations().ugettext(s),
                ngettext=lambda s, p, n: get_translations().ungettext(s, p, n),
                newstyle=True,
                pgettext=lambda c, s: get_translations().upgettext(c, s),
                npgettext=lambda c, s, p, n: get_translations().unpgettext(c, s, p, n),
            )

    def list_translations(self):
        """Returns a list of all the locales translations exist for. The list
        returned will be filled with actual locale objects and not just strings.

        .. note::

            The default locale will always be returned, even if no translation
            files exist for it.

        .. versionadded:: 0.6
        """
        result = []

        for dirname in get_babel().translation_directories:
            if not os.path.isdir(dirname):
                continue

            for folder in os.listdir(dirname):
                locale_dir = os.path.join(dirname, folder, "LC_MESSAGES")
                if not os.path.isdir(locale_dir):
                    continue

                if any(x.endswith(".mo") for x in os.listdir(locale_dir)):
                    result.append(Locale.parse(folder))

        if self.default_locale not in result:
            result.append(self.default_locale)
        return result

    @property
    def default_locale(self) -> Locale:
        """The default locale from the configuration as an instance of a
        `babel.Locale` object.
        """
        return Locale.parse(get_babel().default_locale)

    @property
    def default_timezone(self) -> timezone:
        """The default timezone from the configuration as an instance of a
        `pytz.timezone` object.
        """
        return timezone(get_babel().default_timezone)

    @property
    def domain(self) -> str:
        """The message domain for the translations as a string."""
        return get_babel().default_domain

    @cached_property
    def domain_instance(self):
        """The message domain for the translations."""
        return Domain(domain=self.domain)

    @staticmethod
    def _resolve_directories(directories: List[str], app=None):
        for path in directories:
            if os.path.isabs(path):
                yield path
            elif app is not None:
                # We can only resolve relative paths if we have an application
                # context.
                yield os.path.join(app.root_path, path)


def get_translations() -> Union[Translations, NullTranslations]:
    """Returns the correct gettext translations that should be used for
    this request.  This will never fail and return a dummy translation
    object if used outside the request or if a translation cannot be found.
    """
    return get_domain().get_translations()


def get_locale() -> Optional[Locale]:
    """Returns the locale that should be used for this request as
    `babel.Locale` object.  This returns `None` if used outside a request.
    """
    ctx = _get_current_context()
    if ctx is None:
        return None

    locale = getattr(ctx, "babel_locale", None)
    if locale is None:
        babel = get_babel()
        if babel.locale_selector is None:
            locale = babel.instance.default_locale
        else:
            rv = babel.locale_selector()
            if rv is None:
                locale = babel.instance.default_locale
            else:
                locale = Locale.parse(rv)
        ctx.babel_locale = locale

    return locale


def get_timezone() -> Optional[timezone]:
    """Returns the timezone that should be used for this request as
    a `pytz.timezone` object.  This returns `None` if used outside a request.
    """
    ctx = _get_current_context()
    tzinfo = getattr(ctx, "babel_tzinfo", None)
    if tzinfo is None:
        babel = get_babel()
        if babel.timezone_selector is None:
            tzinfo = babel.instance.default_timezone
        else:
            rv = babel.timezone_selector()
            if rv is None:
                tzinfo = babel.instance.default_timezone
            else:
                tzinfo = timezone(rv) if isinstance(rv, str) else rv
        ctx.babel_tzinfo = tzinfo
    return tzinfo


def refresh():
    """Refreshes the cached timezones and locale information.  This can
    be used to switch a translation between a request and if you want
    the changes to take place immediately, not just with the next request::

        user.timezone = request.form['timezone']
        user.locale = request.form['locale']
        refresh()
        flash(gettext('Language was changed'))

    Without that refresh, the :func:`~flask.flash` function would probably
    return English text and a now German page.
    """
    ctx = _get_current_context()
    for key in "babel_locale", "babel_tzinfo", "babel_translations":
        if hasattr(ctx, key):
            delattr(ctx, key)

    if hasattr(ctx, "forced_babel_locale"):
        ctx.babel_locale = ctx.forced_babel_locale


@contextmanager
def force_locale(locale):
    """Temporarily overrides the currently selected locale.

    Sometimes it is useful to switch the current locale to different one, do
    some tasks and then revert back to the original one. For example, if the
    user uses German on the website, but you want to email them in English,
    you can use this function as a context manager::

        with force_locale('en_US'):
            send_email(gettext('Hello!'), ...)

    :param locale: The locale to temporary switch to (ex: 'en_US').
    """
    ctx = _get_current_context()
    if ctx is None:
        yield
        return

    orig_attrs = {}
    for key in ("babel_translations", "babel_locale"):
        orig_attrs[key] = getattr(ctx, key, None)

    try:
        ctx.babel_locale = Locale.parse(locale)
        ctx.forced_babel_locale = ctx.babel_locale
        ctx.babel_translations = None
        yield
    finally:
        if hasattr(ctx, "forced_babel_locale"):
            del ctx.forced_babel_locale

        for key, value in orig_attrs.items():
            setattr(ctx, key, value)


def _get_format(key, format) -> Optional[str]:
    """A small helper for the datetime formatting functions.  Looks up
    format defaults for different kinds.
    """
    babel = get_babel()
    if format is None:
        format = babel.instance.date_formats[key]
    if format in ("short", "medium", "full", "long"):
        rv = babel.instance.date_formats["%s.%s" % (key, format)]
        if rv is not None:
            format = rv
    return format


def to_user_timezone(datetime):
    """Convert a datetime object to the user's timezone.  This automatically
    happens on all date formatting unless rebasing is disabled.  If you need
    to convert a :class:`datetime.datetime` object at any time to the user's
    timezone (as returned by :func:`get_timezone`) this function can be used.
    """
    if datetime.tzinfo is None:
        datetime = datetime.replace(tzinfo=UTC)
    tzinfo = get_timezone()
    return tzinfo.normalize(datetime.astimezone(tzinfo))


def to_utc(datetime):
    """Convert a datetime object to UTC and drop tzinfo.  This is the
    opposite operation to :func:`to_user_timezone`.
    """
    if datetime.tzinfo is None:
        datetime = get_timezone().localize(datetime)
    return datetime.astimezone(UTC).replace(tzinfo=None)


def format_datetime(datetime=None, format=None, rebase=True):
    """Return a date formatted according to the given pattern.  If no
    :class:`~datetime.datetime` object is passed, the current time is
    assumed.  By default, rebasing happens, which causes the object to
    be converted to the user's timezone (as returned by
    :func:`to_user_timezone`).  This function formats both date and
    time.

    The format parameter can either be ``'short'``, ``'medium'``,
    ``'long'`` or ``'full'`` (in which case the language's default for
    that setting is used, or the default from the :attr:`Babel.date_formats`
    mapping is used) or a format string as documented by Babel.

    This function is also available in the template context as filter
    named `datetimeformat`.
    """
    format = _get_format("datetime", format)
    return _date_format(dates.format_datetime, datetime, format, rebase)


def format_date(date=None, format=None, rebase=True):
    """Return a date formatted according to the given pattern.  If no
    :class:`~datetime.datetime` or :class:`~datetime.date` object is passed,
    the current time is assumed.  By default, rebasing happens, which causes
    the object to be converted to the users's timezone (as returned by
    :func:`to_user_timezone`).  This function only formats the date part
    of a :class:`~datetime.datetime` object.

    The format parameter can either be ``'short'``, ``'medium'``,
    ``'long'`` or ``'full'`` (in which case the language's default for
    that setting is used, or the default from the :attr:`Babel.date_formats`
    mapping is used) or a format string as documented by Babel.

    This function is also available in the template context as filter
    named `dateformat`.
    """
    if rebase and isinstance(date, datetime):
        date = to_user_timezone(date)
    format = _get_format("date", format)
    return _date_format(dates.format_date, date, format, rebase)


def format_time(time=None, format=None, rebase=True):
    """Return a time formatted according to the given pattern.  If no
    :class:`~datetime.datetime` object is passed, the current time is
    assumed.  By default, rebasing happens, which causes the object to
    be converted to the user's timezone (as returned by
    :func:`to_user_timezone`).  This function formats both date and time.

    The format parameter can either be ``'short'``, ``'medium'``,
    ``'long'`` or ``'full'`` (in which case the language's default for
    that setting is used, or the default from the :attr:`Babel.date_formats`
    mapping is used) or a format string as documented by Babel.

    This function is also available in the template context as filter
    named `timeformat`.
    """
    format = _get_format("time", format)
    return _date_format(dates.format_time, time, format, rebase)


def format_timedelta(
    datetime_or_timedelta,
    granularity: str = "second",
    add_direction=False,
    threshold=0.85,
):
    """Format the elapsed time from the given date to now or the given
    timedelta.

    This function is also available in the template context as filter
    named `timedeltaformat`.
    """
    if isinstance(datetime_or_timedelta, datetime):
        datetime_or_timedelta = datetime.utcnow() - datetime_or_timedelta
    return dates.format_timedelta(
        datetime_or_timedelta,
        granularity,
        threshold=threshold,
        add_direction=add_direction,
        locale=get_locale(),
    )


def _date_format(formatter, obj, format, rebase, **extra):
    """Internal helper that formats the date."""
    locale = get_locale()
    extra = {}
    if formatter is not dates.format_date and rebase:
        extra["tzinfo"] = get_timezone()
    return formatter(obj, format, locale=locale, **extra)


def format_number(number) -> str:
    """Return the given number formatted for the locale in request

    :param number: the number to format
    :return: the formatted number
    :rtype: unicode
    """
    locale = get_locale()
    return numbers.format_decimal(number, locale=locale)


def format_decimal(number, format=None) -> str:
    """Return the given decimal number formatted for the locale in the request.

    :param number: the number to format
    :param format: the format to use
    :return: the formatted number
    :rtype: unicode
    """
    locale = get_locale()
    return numbers.format_decimal(number, format=format, locale=locale)


def format_currency(
    number, currency, format=None, currency_digits=True, format_type="standard"
) -> str:
    """Return the given number formatted for the locale in the request.

    :param number: the number to format
    :param currency: the currency code
    :param format: the format to use
    :param currency_digits: use the currency’s number of decimal digits
                            [default: True]
    :param format_type: the currency format type to use
                        [default: standard]
    :return: the formatted number
    :rtype: unicode
    """
    locale = get_locale()
    return numbers.format_currency(
        number,
        currency,
        format=format,
        locale=locale,
        currency_digits=currency_digits,
        format_type=format_type,
    )


def format_percent(number, format=None) -> str:
    """Return formatted percent value for the locale in the request.

    :param number: the number to format
    :param format: the format to use
    :return: the formatted percent number
    :rtype: unicode
    """
    locale = get_locale()
    return numbers.format_percent(number, format=format, locale=locale)


def format_scientific(number, format=None) -> str:
    """Return value formatted in scientific notation for the locale in request

    :param number: the number to format
    :param format: the format to use
    :return: the formatted percent number
    :rtype: unicode
    """
    locale = get_locale()
    return numbers.format_scientific(number, format=format, locale=locale)


class Domain(object):
    """Localization domain. By default, it will look for translations in the
    Flask application directory and "messages" domain - all message catalogs
    should be called ``messages.mo``.

    Additional domains are supported passing a list of domain names to the
    ``domain`` argument, but note that in this case they must match a list
    passed to ``translation_directories``, eg::

        Domain(
            translation_directories=[
                "/path/to/translations/with/messages/domain",
                "/another/path/to/translations/with/another/domain",
            ],
            domains=[
                "messages",
                "myapp",
            ]
        )
    """

    def __init__(self, translation_directories=None, plugin_translation_packages=None, domain="messages"):
        if isinstance(translation_directories, str):
            translation_directories = [translation_directories]
        self._translation_directories = translation_directories

        if isinstance(plugin_translation_packages, str):
            plugin_translation_packages = [plugin_translation_packages]
        self._plugin_translation_packages = plugin_translation_packages

        self.domain = domain.split(";")

        self.cache = {}

    def __repr__(self):
        return "<Domain({!r}, {!r})>".format(self._translation_directories, self.domain)

    @property
    def translation_directories(self):
        if self._translation_directories is not None:
            return self._translation_directories
        return get_babel().translation_directories

    @property
    def plugin_translation_packages(self):
        if self._plugin_translation_packages is not None:
            return self._plugin_translation_packages
        return get_babel().plugin_translation_packages

    def as_default(self):
        """Set this domain as default for the current request"""
        ctx = _get_current_context()

        if ctx is None:
            raise RuntimeError("No request context")

        ctx.babel_domain = self

    def get_translations_cache(self, ctx):
        """Returns dictionary-like object for translation caching"""
        return self.cache

    def get_translations(self):
        ctx = _get_current_context()

        if ctx is None:
            return support.NullTranslations()

        cache = self.get_translations_cache(ctx)
        locale = get_locale()
        try:
            return cache[str(locale), self.domain[0]]
        except KeyError:
            translations = support.Translations()

            for index, dirname in enumerate(self.translation_directories):

                domain = self.domain[0] if len(self.domain) == 1 else self.domain[index]

                catalog = support.Translations.load(dirname, [locale], domain)
                translations.merge(catalog)
                # FIXME: Workaround for merge() being really, really stupid. It
                # does not copy _info, plural(), or any other instance variables
                # populated by GNUTranslations. We probably want to stop using
                # `support.Translations.merge` entirely.
                if catalog.info() and hasattr(catalog, "plural"):
                    translations.plural = catalog.plural
            for pkg in self.plugin_translation_packages:
                try:
                    mo_path = (
                        resources.files(pkg)
                        .joinpath(f"translations/{locale}/LC_MESSAGES/{self.domain[0]}.mo")
                    )
                    with mo_path.open("rb") as f:
                        plugin_trans = GNUTranslations(f)
                        translations.merge(plugin_trans)

                        # 同样处理 plural 问题
                        if plugin_trans.info() and hasattr(plugin_trans, "plural"):
                            translations.plural = plugin_trans.plural
                except FileNotFoundError:
                    continue
                except Exception as e:
                    print(f"[plugin translation] failed to load from {pkg}: {e}")

            cache[str(locale), self.domain[0]] = translations
            return translations

    def gettext(self, string, **variables):
        """Translates a string with the current locale and passes in the
        given keyword arguments as mapping to a string formatting string.

        ::

            gettext(u'Hello World!')
            gettext(u'Hello %(name)s!', name='World')
        """
        t = self.get_translations()
        s = t.ugettext(string)
        return s if not variables else s % variables

    def ngettext(self, singular, plural, num, **variables):
        """Translates a string with the current locale and passes in the
        given keyword arguments as mapping to a string formatting string.
        The `num` parameter is used to dispatch between singular and various
        plural forms of the message.  It is available in the format string
        as ``%(num)d`` or ``%(num)s``.  The source language should be
        English or a similar language which only has one plural form.

        ::

            ngettext(u'%(num)d Apple', u'%(num)d Apples', num=len(apples))
        """
        variables.setdefault("num", num)
        t = self.get_translations()
        s = t.ungettext(singular, plural, num)
        return s if not variables else s % variables

    def pgettext(self, context, string, **variables):
        """Like :func:`gettext` but with a context.

        .. versionadded:: 0.7
        """
        t = self.get_translations()
        s = t.upgettext(context, string)
        return s if not variables else s % variables

    def npgettext(self, context, singular, plural, num, **variables):
        """Like :func:`ngettext` but with a context.

        .. versionadded:: 0.7
        """
        variables.setdefault("num", num)
        t = self.get_translations()
        s = t.unpgettext(context, singular, plural, num)
        return s if not variables else s % variables

    def lazy_gettext(self, string, **variables):
        """Like :func:`gettext` but the string returned is lazy which means
        it will be translated when it is used as an actual string.

        Example::

            hello = lazy_gettext(u'Hello World')

            @app.route('/')
            def index():
                return unicode(hello)
        """
        return LazyString(self.gettext, string, **variables)

    def lazy_ngettext(self, singular, plural, num, **variables):
        """Like :func:`ngettext` but the string returned is lazy which means
        it will be translated when it is used as an actual string.

        Example::

            apples = lazy_ngettext(
                u'%(num)d Apple',
                u'%(num)d Apples',
                num=len(apples)
            )

            @app.route('/')
            def index():
                return unicode(apples)
        """
        return LazyString(self.ngettext, singular, plural, num, **variables)

    def lazy_pgettext(self, context, string, **variables):
        """Like :func:`pgettext` but the string returned is lazy which means
        it will be translated when it is used as an actual string.

        .. versionadded:: 0.7
        """
        return LazyString(self.pgettext, context, string, **variables)


def _get_current_context() -> Optional[SimpleNamespace]:
    if not g:
        return None

    if not hasattr(g, "_flask_babel"):
        g._flask_babel = SimpleNamespace()

    return g._flask_babel  # noqa


def get_domain() -> Domain:
    ctx = _get_current_context()
    if ctx is None:
        # this will use NullTranslations
        return Domain()

    try:
        return ctx.babel_domain
    except AttributeError:
        pass

    ctx.babel_domain = get_babel().instance.domain_instance
    return ctx.babel_domain


# Create shortcuts for the default Flask domain
def gettext(*args, **kwargs) -> str:
    return get_domain().gettext(*args, **kwargs)


_ = gettext


def ngettext(*args, **kwargs) -> str:
    return get_domain().ngettext(*args, **kwargs)


def pgettext(*args, **kwargs) -> str:
    return get_domain().pgettext(*args, **kwargs)


def npgettext(*args, **kwargs) -> str:
    return get_domain().npgettext(*args, **kwargs)


def lazy_gettext(*args, **kwargs) -> LazyString:
    return LazyString(gettext, *args, **kwargs)


def lazy_pgettext(*args, **kwargs) -> LazyString:
    return LazyString(pgettext, *args, **kwargs)


def lazy_ngettext(*args, **kwargs) -> LazyString:
    return LazyString(ngettext, *args, **kwargs)


def lazy_npgettext(*args, **kwargs) -> LazyString:
    return LazyString(npgettext, *args, **kwargs)
