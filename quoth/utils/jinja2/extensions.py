from jinja2 import nodes
from jinja2.ext import Extension
from django.conf import settings

class StaticURLExtension(Extension):
    tags = set(['static_url'])

    def parse(self, parser):
        token = parser.stream.next()
        bits = []
        while not parser.stream.current.type == 'block_end':
            bits.append(parser.stream.next())
        asset = nodes.Const("".join([b.value for b in bits]))
        return nodes.Output([self.call_method('_static_url', args=[asset])]).set_lineno(token.lineno)

    def _static_url(self, asset):
        return ''.join([settings.STATIC_URL, asset])

class URLExtension(Extension):
    """Returns an absolute URL matching given view with its parameters.

    This is a way to define links that aren't tied to a particular URL
    configuration::

        {% url path.to.some_view arg1,arg2,name1=value1 %}

    Known differences to Django's url-Tag:

        - In Django, the view name may contain any non-space character.
          Since Jinja's lexer does not identify whitespace to us, only
          characters that make up valid identifers, plus dots and hyphens
          are allowed. Note that identifers in Jinja 2 may not contain
          non-ascii characters.

          As an alternative, you may specifify the view as a string,
          which bypasses all these restrictions. It further allows you
          to apply filters:

            {% url "model.some-view"|afilter %}
    """

    tags = set(['url'])

    def parse(self, parser):
        stream = parser.stream

        tag = stream.next()

        # get view name
        if stream.current.test('string'):
            viewname = parser.parse_primary()
        else:
            # parse valid tokens and manually build a string from them
            bits = []
            name_allowed = True
            while True:
                if stream.current.test_any('dot', 'sub'):
                    bits.append(stream.next())
                    name_allowed = True
                elif stream.current.test('name') and name_allowed:
                    bits.append(stream.next())
                    name_allowed = False
                else:
                    break
            viewname = nodes.Const("".join([b.value for b in bits]))
            if not bits:
                raise TemplateSyntaxError("'%s' requires path to view" %
                    tag.value, tag.lineno)

        # get arguments
        args = []
        kwargs = []
        while not stream.current.test_any('block_end', 'name:as'):
            if args or kwargs:
                stream.expect('comma')
            if stream.current.test('name') and stream.look().test('assign'):
                key = nodes.Const(stream.next().value)
                stream.skip()
                value = parser.parse_expression()
                kwargs.append(nodes.Pair(key, value, lineno=key.lineno))
            else:
                args.append(parser.parse_expression())

        make_call_node = lambda *kw: \
            self.call_method('_reverse',
                             args=[viewname, nodes.List(args), nodes.Dict(kwargs)],
                             kwargs=kw)

        # if an as-clause is specified, write the result to context...
        if stream.next_if('name:as'):
            var = nodes.Name(stream.expect('name').value, 'store')
            call_node = make_call_node(nodes.Keyword('fail', nodes.Const(False)))
            return nodes.Assign(var, call_node)
        # ...otherwise print it out.
        else:
            return nodes.Output([make_call_node()]).set_lineno(tag.lineno)

    @classmethod
    def _reverse(self, viewname, args, kwargs, fail=True):
        from django.core.urlresolvers import reverse, NoReverseMatch

        # Try to look up the URL twice: once given the view name,
        # and again relative to what we guess is the "main" app.
        url = ''
        try:
            url = reverse(viewname, args=args, kwargs=kwargs)
        except NoReverseMatch:
            projectname = settings.SETTINGS_MODULE.split('.')[0]
            try:
                url = reverse(projectname + '.' + viewname,
                              args=args, kwargs=kwargs)
            except NoReverseMatch:
                if fail:
                    raise
                else:
                    return ''

        return url


class WithExtension(Extension):
    """Adds a value to the context (inside this block) for caching and
    easy access, just like the Django-version does.

    For example::

        {% with person.some_sql_method as total %}
            {{ total }} object{{ total|pluralize }}
        {% endwith %}

    TODO: The new Scope node introduced in Jinja2 6334c1eade73 (the 2.2
    dev version) would help here, but we don't want to rely on that yet.
    See also:
        http://dev.pocoo.org/projects/jinja/browser/tests/test_ext.py
        http://dev.pocoo.org/projects/jinja/ticket/331
        http://dev.pocoo.org/projects/jinja/ticket/329
    """

    tags = set(['with'])

    def parse(self, parser):
        lineno = parser.stream.next().lineno
        value = parser.parse_expression()
        parser.stream.expect('name:as')
        name = parser.stream.expect('name')
        body = parser.parse_statements(['name:endwith'], drop_needle=True)
        # Use a local variable instead of a macro argument to alias  
        # the expression.  This allows us to nest "with" statements.
        body.insert(0, nodes.Assign(nodes.Name(name.value, 'store'), value))
        return nodes.CallBlock(
                self.call_method('_render_block'), [], [], body).\
                    set_lineno(lineno)

    def _render_block(self, caller=None):
        return caller()


class CacheExtension(Extension):
    """Exactly like Django's own tag, but supports full Jinja2
    expressiveness for all arguments.

        {% cache gettimeout()*2 "foo"+options.cachename  %}
            ...
        {% endcache %}

    This actually means that there is a considerable incompatibility
    to Django: In Django, the second argument is simply a name, but
    interpreted as a literal string. This tag, with Jinja2 stronger
    emphasis on consistent syntax, requires you to actually specify the
    quotes around the name to make it a string. Otherwise, allowing
    Jinja2 expressions would be very hard to impossible (one could use
    a lookahead to see if the name is followed by an operator, and
    evaluate it as an expression if so, or read it as a string if not.
    TODO: This may not be the right choice. Supporting expressions
    here is probably not very important, so compatibility should maybe
    prevail. Unfortunately, it is actually pretty hard to be compatibly
    in all cases, simply because Django's per-character parser will
    just eat everything until the next whitespace and consider it part
    of the fragment name, while we have to work token-based: ``x*2``
    would actually be considered ``"x*2"`` in Django, while Jinja2
    would give us three tokens: ``x``, ``*``, ``2``.

    General Syntax:

        {% cache [expire_time] [fragment_name] [var1] [var2] .. %}
            .. some expensive processing ..
        {% endcache %}

    Available by default (does not need to be loaded).

    Partly based on the ``FragmentCacheExtension`` from the Jinja2 docs.

    TODO: Should there be scoping issues with the internal dummy macro
    limited access to certain outer variables in some cases, there is a
    different way to write this. Generated code would look like this:

        internal_name = environment.extensions['..']._get_cache_value():
        if internal_name is not None:
            yield internal_name
        else:
            internal_name = ""  # or maybe use [] and append() for performance
            internalname += "..."
            internalname += "..."
            internalname += "..."
            environment.extensions['..']._set_cache_value(internalname):
            yield internalname

    In other words, instead of using a CallBlock which uses a local
    function and calls into python, we have to separate calls into
    python, but put the if-else logic itself into the compiled template.
    """

    tags = set(['cache'])

    def parse(self, parser):
        lineno = parser.stream.next().lineno

        expire_time = parser.parse_expression()
        fragment_name = parser.parse_expression()
        vary_on = []
        while not parser.stream.current.test('block_end'):
            vary_on.append(parser.parse_expression())

        body = parser.parse_statements(['name:endcache'], drop_needle=True)

        return nodes.CallBlock(
            self.call_method('_cache_support',
                             [expire_time, fragment_name,
                              nodes.List(vary_on), nodes.Const(lineno)]),
            [], [], body).set_lineno(lineno)

    def _cache_support(self, expire_time, fragm_name, vary_on, lineno, caller):
        from django.core.cache import cache   # delay depending in settings
        from django.utils.http import urlquote
        from django.utils.hashcompat import md5_constructor

        try:
            expire_time = int(expire_time)
        except (ValueError, TypeError):
            raise TemplateSyntaxError('"%s" tag got a non-integer '
                'timeout value: %r' % (list(self.tags)[0], expire_time), lineno)

        args_string = u':'.join([urlquote(v) for v in vary_on])
        args_md5 = md5_constructor(args_string)
        cache_key = 'template.cache.%s.%s' % (fragm_name, args_md5.hexdigest())
        value = cache.get(cache_key)
        if value is None:
            value = caller()
            cache.set(cache_key, value, expire_time)
        return value


class SpacelessExtension(Extension):
    """Removes whitespace between HTML tags, including tab and
    newline characters.

    Works exactly like Django's own tag.
    """

    tags = set(['spaceless'])

    def parse(self, parser):
        lineno = parser.stream.next().lineno
        body = parser.parse_statements(['name:endspaceless'], drop_needle=True)
        return nodes.CallBlock(
            self.call_method('_strip_spaces', [], [], None, None),
            [], [], body
        ).set_lineno(lineno)

    def _strip_spaces(self, caller=None):
        from django.utils.html import strip_spaces_between_tags
        return strip_spaces_between_tags(caller().strip())


class CsrfTokenExtension(Extension):
    """Jinja2-version of the ``csrf_token`` tag.

    Adapted from a snippet by Jason Green:
    http://www.djangosnippets.org/snippets/1847/

    This tag is a bit stricter than the Django tag in that it doesn't
    simply ignore any invalid arguments passed in.
    """

    tags = set(['csrf_token'])

    def parse(self, parser):
        lineno = parser.stream.next().lineno
        return nodes.Output([
            self.call_method('_render', [nodes.Name('csrf_token', 'load')])
        ]).set_lineno(lineno)

    def _render(self, csrf_token):
        from django.template.defaulttags import CsrfTokenNode
        return Markup(CsrfTokenNode().render({'csrf_token': csrf_token}))

# nicer import names
url = URLExtension
with_ = WithExtension
cache = CacheExtension
spaceless = SpacelessExtension
csrf_token = CsrfTokenExtension
static_url = StaticURLExtension
