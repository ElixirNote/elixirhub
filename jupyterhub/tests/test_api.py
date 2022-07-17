"""Tests for the REST API."""
import asyncio
import json
import re
import sys
import uuid
from datetime import datetime
from datetime import timedelta
from unittest import mock
from urllib.parse import quote
from urllib.parse import urlparse
from urllib.parse import urlunparse

from pytest import fixture
from pytest import mark
from tornado.httputil import url_concat

import jupyterhub
from .. import orm
from ..apihandlers.base import PAGINATION_MEDIA_TYPE
from ..objects import Server
from ..utils import url_path_join as ujoin
from ..utils import utcnow
from .conftest import new_username
from .mocking import public_host
from .mocking import public_url
from .utils import add_user
from .utils import api_request
from .utils import async_requests
from .utils import auth_header
from .utils import find_user


# --------------------
# Authentication tests
# --------------------


async def test_auth_api(app):
    db = app.db
    r = await api_request(app, 'authorizations', 'gobbledygook')
    assert r.status_code == 404

    # make a new cookie token
    user = find_user(db, 'admin')
    api_token = user.new_api_token()

    # check success:
    r = await api_request(app, 'authorizations/token', api_token)
    assert r.status_code == 200
    reply = r.json()
    assert reply['name'] == user.name

    # check fail
    r = await api_request(
        app, 'authorizations/token', api_token, headers={'Authorization': 'no sir'}
    )
    assert r.status_code == 403

    r = await api_request(
        app,
        'authorizations/token',
        api_token,
        headers={'Authorization': 'token: %s' % user.cookie_id},
    )
    assert r.status_code == 403


@mark.parametrize(
    "content_type, status",
    [
        ("text/plain", 403),
        # accepted, but invalid
        ("application/json; charset=UTF-8", 400),
    ],
)
async def test_post_content_type(app, content_type, status):
    url = ujoin(public_host(app), app.hub.base_url)
    host = urlparse(url).netloc
    # add admin user
    user = find_user(app.db, 'admin')
    if user is None:
        user = add_user(app.db, name='admin', admin=True)
    cookies = await app.login_user('admin')

    r = await api_request(
        app,
        'users',
        method='post',
        data='{}',
        headers={
            "Authorization": "",
            "Content-Type": content_type,
        },
        cookies=cookies,
    )
    assert r.status_code == status


@mark.parametrize(
    "host, referer, extraheaders, status",
    [
        ('$host', '$url', {}, 200),
        (None, None, {}, 200),
        (None, 'null', {}, 403),
        (None, 'http://attack.com/csrf/vulnerability', {}, 403),
        ('$host', {"path": "/user/someuser"}, {}, 403),
        ('$host', {"path": "{path}/foo/bar/subpath"}, {}, 200),
        # mismatch host
        ("mismatch.com", "$url", {}, 403),
        # explicit host, matches
        ("fake.example", {"netloc": "fake.example"}, {}, 200),
        # explicit port, matches implicit port
        ("fake.example:80", {"netloc": "fake.example"}, {}, 200),
        # explicit port, mismatch
        ("fake.example:81", {"netloc": "fake.example"}, {}, 403),
        # implicit ports, mismatch proto
        ("fake.example", {"netloc": "fake.example", "scheme": "https"}, {}, 403),
        # explicit ports, match
        ("fake.example:81", {"netloc": "fake.example:81"}, {}, 200),
        # Test proxy protocol defined headers taken into account by utils.get_browser_protocol
        (
            "fake.example",
            {"netloc": "fake.example", "scheme": "https"},
            {'X-Scheme': 'https'},
            200,
        ),
        (
            "fake.example",
            {"netloc": "fake.example", "scheme": "https"},
            {'X-Forwarded-Proto': 'https'},
            200,
        ),
        (
            "fake.example",
            {"netloc": "fake.example", "scheme": "https"},
            {
                'Forwarded': 'host=fake.example;proto=https,for=1.2.34;proto=http',
                'X-Scheme': 'http',
            },
            200,
        ),
        (
            "fake.example",
            {"netloc": "fake.example", "scheme": "https"},
            {
                'Forwarded': 'host=fake.example;proto=http,for=1.2.34;proto=http',
                'X-Scheme': 'https',
            },
            403,
        ),
        ("fake.example", {"netloc": "fake.example"}, {'X-Scheme': 'https'}, 403),
        ("fake.example", {"netloc": "fake.example"}, {'X-Scheme': 'https, http'}, 403),
    ],
)
async def test_cors_check(request, app, host, referer, extraheaders, status):
    url = ujoin(public_host(app), app.hub.base_url)
    real_host = urlparse(url).netloc
    if host == "$host":
        host = real_host

    if referer == '$url':
        referer = url
    elif isinstance(referer, dict):
        parsed_url = urlparse(url)
        # apply {}
        url_ns = {key: getattr(parsed_url, key) for key in parsed_url._fields}
        for key, value in referer.items():
            referer[key] = value.format(**url_ns)
        referer = urlunparse(parsed_url._replace(**referer))

    # disable default auth header, cors is for cookie auth
    headers = {"Authorization": ""}
    if host is not None:
        headers['X-Forwarded-Host'] = host
    if referer is not None:
        headers['Referer'] = referer
    headers.update(extraheaders)

    # add admin user
    user = find_user(app.db, 'admin')
    if user is None:
        user = add_user(app.db, name='admin', admin=True)
    cookies = await app.login_user('admin')

    # test custom forwarded_host_header behavior
    app.forwarded_host_header = 'X-Forwarded-Host'

    # reset the config after the test to avoid leaking state
    def reset_header():
        app.forwarded_host_header = ""

    request.addfinalizer(reset_header)

    r = await api_request(
        app,
        'users',
        headers=headers,
        cookies=cookies,
    )
    assert r.status_code == status


# --------------
# User API tests
# --------------


def normalize_timestamp(ts):
    """Normalize a timestamp

    For easier comparison
    """
    if ts is None:
        return
    return re.sub(r'\d(\.\d+)?', '0', ts)


def normalize_user(user):
    """Normalize a user model for comparison

    smooths out user model with things like timestamps
    for easier comparison
    """
    for key in ('created', 'last_activity'):
        user[key] = normalize_timestamp(user[key])
    if 'roles' in user:
        user['roles'] = sorted(user['roles'])
    if 'servers' in user:
        for server in user['servers'].values():
            for key in ('started', 'last_activity'):
                server[key] = normalize_timestamp(server[key])
            server['progress_url'] = re.sub(
                r'.*/hub/api', 'PREFIX/hub/api', server['progress_url']
            )
            if isinstance(server['state'], dict) and isinstance(
                server['state'].get('pid', None), int
            ):
                server['state']['pid'] = 0
    return user


def fill_user(model):
    """Fill a default user model

    Any unspecified fields will be filled with the defaults
    """
    model.setdefault('server', None)
    model.setdefault('kind', 'user')
    model.setdefault('roles', [])
    model.setdefault('groups', [])
    model.setdefault('admin', False)
    model.setdefault('pending', None)
    model.setdefault('created', TIMESTAMP)
    model.setdefault('last_activity', TIMESTAMP)
    model.setdefault('servers', {})
    return model


TIMESTAMP = normalize_timestamp(datetime.now().isoformat() + 'Z')


@mark.user
@mark.role
async def test_get_users(app):
    db = app.db

    r = await api_request(app, 'users', headers=auth_header(db, 'admin'))
    assert r.status_code == 200

    users = sorted(r.json(), key=lambda d: d['name'])
    users = [normalize_user(u) for u in users]
    user_model = {
        'name': 'user',
        'admin': False,
        'roles': ['user'],
        'auth_state': None,
    }
    assert users == [
        fill_user(
            {
                'name': 'admin',
                'admin': True,
                'roles': ['admin', 'user'],
                'auth_state': None,
            }
        ),
        fill_user(user_model),
    ]
    r = await api_request(app, 'users', headers=auth_header(db, 'user'))
    assert r.status_code == 403


@fixture
def default_page_limit(app):
    """Set and return low default page size for testing"""
    n = 10
    with mock.patch.dict(app.tornado_settings, {"api_page_default_limit": n}):
        yield n


@fixture
def max_page_limit(app):
    """Set and return low max page size for testing"""
    n = 20
    with mock.patch.dict(app.tornado_settings, {"api_page_max_limit": n}):
        yield n


@mark.user
@mark.role
@mark.parametrize(
    "n, offset, limit, accepts_pagination, expected_count",
    [
        (10, None, None, False, 10),
        (10, None, None, True, 10),
        (10, 5, None, True, 5),
        (10, 5, None, False, 5),
        (10, 5, 1, True, 1),
        (10, 10, 10, True, 0),
        (  # default page limit, pagination expected
            30,
            None,
            None,
            True,
            'default',
        ),
        (
            # default max page limit, pagination not expected
            30,
            None,
            None,
            False,
            'max',
        ),
        (
            # limit exceeded
            30,
            None,
            500,
            False,
            'max',
        ),
    ],
)
async def test_get_users_pagination(
    app,
    n,
    offset,
    limit,
    accepts_pagination,
    expected_count,
    default_page_limit,
    max_page_limit,
):
    db = app.db

    if expected_count == 'default':
        expected_count = default_page_limit
    elif expected_count == 'max':
        expected_count = max_page_limit
    # populate users
    usernames = []

    existing_users = db.query(orm.User).order_by(orm.User.id.asc())
    usernames.extend(u.name for u in existing_users)

    for i in range(n - existing_users.count()):
        name = new_username()
        usernames.append(name)
        add_user(db, app, name=name)
    print(f"{db.query(orm.User).count()} total users")

    url = 'users'
    params = {}
    if offset:
        params['offset'] = offset
    if limit:
        params['limit'] = limit
    url = url_concat(url, params)
    headers = auth_header(db, 'admin')
    if accepts_pagination:
        headers['Accept'] = PAGINATION_MEDIA_TYPE
    r = await api_request(app, url, headers=headers)
    assert r.status_code == 200
    response = r.json()
    if accepts_pagination:
        assert set(response) == {
            "items",
            "_pagination",
        }
        pagination = response["_pagination"]
        users = response["items"]
    else:
        users = response
    assert len(users) == expected_count
    expected_usernames = usernames
    if offset:
        expected_usernames = expected_usernames[offset:]
    expected_usernames = expected_usernames[:expected_count]

    got_usernames = [u['name'] for u in users]
    assert got_usernames == expected_usernames


@mark.user
@mark.parametrize(
    "state",
    ("inactive", "active", "ready", "invalid"),
)
async def test_get_users_state_filter(app, state):
    db = app.db

    # has_one_active: one active, one inactive, zero ready
    has_one_active = add_user(db, app=app, name='has_one_active')
    # has_two_active: two active, ready servers
    has_two_active = add_user(db, app=app, name='has_two_active')
    # has_two_inactive: two spawners, neither active
    has_two_inactive = add_user(db, app=app, name='has_two_inactive')
    # has_zero: no Spawners registered at all
    has_zero = add_user(db, app=app, name='has_zero')

    test_usernames = {
        "has_one_active",
        "has_two_active",
        "has_two_inactive",
        "has_zero",
    }

    user_states = {
        "inactive": ["has_two_inactive", "has_zero"],
        "ready": ["has_two_active"],
        "active": ["has_one_active", "has_two_active"],
        "invalid": [],
    }
    expected = user_states[state]

    def add_spawner(user, name='', active=True, ready=True):
        """Add a spawner in a requested state

        If active, should turn up in an active query
        If active and ready, should turn up in a ready query
        If not active, should turn up in an inactive query
        """
        spawner = user.spawners[name]
        db.commit()
        if active:
            orm_server = orm.Server()
            db.add(orm_server)
            db.commit()
            spawner.server = Server(orm_server=orm_server)
            db.commit()
            if not ready:
                spawner._spawn_pending = True
        return spawner

    for name in ("", "secondary"):
        add_spawner(has_two_active, name, active=True)
        add_spawner(has_two_inactive, name, active=False)

    add_spawner(has_one_active, active=True, ready=False)
    add_spawner(has_one_active, "inactive", active=False)

    r = await api_request(app, f'users?state={state}')
    if state == "invalid":
        assert r.status_code == 400
        return
    assert r.status_code == 200

    usernames = sorted(u["name"] for u in r.json() if u["name"] in test_usernames)
    assert usernames == expected


@mark.user
async def test_get_users_name_filter(app):
    db = app.db

    add_user(db, app=app, name='q')
    add_user(db, app=app, name='qr')
    add_user(db, app=app, name='qrs')
    add_user(db, app=app, name='qrst')
    added_usernames = {'q', 'qr', 'qrs', 'qrst'}

    r = await api_request(app, 'users')
    assert r.status_code == 200
    response_users = [u.get("name") for u in r.json()]
    assert added_usernames.intersection(response_users) == added_usernames

    r = await api_request(app, 'users?name_filter=q')
    assert r.status_code == 200
    response_users = [u.get("name") for u in r.json()]
    assert response_users == ['q', 'qr', 'qrs', 'qrst']

    r = await api_request(app, 'users?name_filter=qr')
    assert r.status_code == 200
    response_users = [u.get("name") for u in r.json()]
    assert response_users == ['qr', 'qrs', 'qrst']

    r = await api_request(app, 'users?name_filter=qrs')
    assert r.status_code == 200
    response_users = [u.get("name") for u in r.json()]
    assert response_users == ['qrs', 'qrst']

    r = await api_request(app, 'users?name_filter=qrst')
    assert r.status_code == 200
    response_users = [u.get("name") for u in r.json()]
    assert response_users == ['qrst']


@mark.user
async def test_get_self(app):
    db = app.db

    # basic get self
    r = await api_request(app, 'user')
    r.raise_for_status()
    assert r.json()['kind'] == 'user'

    # identifying user via oauth token works
    u = add_user(db, app=app, name='orpheus')
    token = uuid.uuid4().hex
    oauth_client = orm.OAuthClient(identifier='eurydice')
    db.add(oauth_client)
    db.commit()
    oauth_token = orm.APIToken(
        user=u.orm_user,
        oauth_client=oauth_client,
        token=token,
    )
    db.add(oauth_token)
    db.commit()
    r = await api_request(
        app,
        'user',
        headers={'Authorization': 'token ' + token},
    )
    r.raise_for_status()
    model = r.json()
    assert model['name'] == u.name

    # invalid auth gets 403
    r = await api_request(
        app,
        'user',
        headers={'Authorization': 'token notvalid'},
    )
    assert r.status_code == 403


async def test_get_self_service(app, mockservice):
    r = await api_request(
        app, "user", headers={"Authorization": f"token {mockservice.api_token}"}
    )
    r.raise_for_status()
    service_info = r.json()

    assert service_info['kind'] == 'service'
    assert service_info['name'] == mockservice.name


@mark.user
@mark.role
async def test_add_user(app):
    db = app.db
    name = 'newuser'
    r = await api_request(app, 'users', name, method='post')
    assert r.status_code == 201
    user = find_user(db, name)
    assert user is not None
    assert user.name == name
    assert not user.admin
    # assert newuser has default 'user' role
    assert orm.Role.find(db, 'user') in user.roles
    assert orm.Role.find(db, 'admin') not in user.roles


@mark.user
@mark.role
async def test_get_user(app):
    name = 'user'
    # get own model
    r = await api_request(app, 'users', name, headers=auth_header(app.db, name))
    r.raise_for_status()
    # admin request
    r = await api_request(
        app,
        'users',
        name,
    )
    r.raise_for_status()

    user = normalize_user(r.json())
    assert user == fill_user({'name': name, 'roles': ['user'], 'auth_state': None})

    # admin request, no such user
    r = await api_request(
        app,
        'users',
        'nosuchuser',
    )
    assert r.status_code == 404

    # unauthorized request, no such user
    r = await api_request(
        app,
        'users',
        'nosuchuser',
        headers=auth_header(app.db, name),
    )
    assert r.status_code == 404

    # unauthorized request for existing user
    r = await api_request(
        app,
        'users',
        'admin',
        headers=auth_header(app.db, name),
    )
    assert r.status_code == 404


@mark.user
async def test_add_multi_user_bad(app):
    r = await api_request(app, 'users', method='post')
    assert r.status_code == 400
    r = await api_request(app, 'users', method='post', data='{}')
    assert r.status_code == 400
    r = await api_request(app, 'users', method='post', data='[]')
    assert r.status_code == 400


@mark.user
async def test_add_multi_user_invalid(app):
    app.authenticator.username_pattern = r'w.*'
    r = await api_request(
        app,
        'users',
        method='post',
        data=json.dumps({'usernames': ['Willow', 'Andrew', 'Tara']}),
    )
    app.authenticator.username_pattern = ''
    assert r.status_code == 400
    assert r.json()['message'] == 'Invalid usernames: andrew, tara'


@mark.user
@mark.role
async def test_add_multi_user(app):
    db = app.db
    names = ['a', 'b']
    r = await api_request(
        app, 'users', method='post', data=json.dumps({'usernames': names})
    )
    assert r.status_code == 201
    reply = r.json()
    r_names = [user['name'] for user in reply]
    assert names == r_names

    for name in names:
        user = find_user(db, name)
        assert user is not None
        assert user.name == name
        assert not user.admin
        # assert default 'user' role added
        assert orm.Role.find(db, 'user') in user.roles
        assert orm.Role.find(db, 'admin') not in user.roles

    # try to create the same users again
    r = await api_request(
        app, 'users', method='post', data=json.dumps({'usernames': names})
    )
    assert r.status_code == 409

    names = ['a', 'b', 'ab']

    # try to create the same users again
    r = await api_request(
        app, 'users', method='post', data=json.dumps({'usernames': names})
    )
    assert r.status_code == 201
    reply = r.json()
    r_names = [user['name'] for user in reply]
    assert r_names == ['ab']


@mark.user
@mark.role
async def test_add_multi_user_admin(app):
    db = app.db
    names = ['c', 'd']
    r = await api_request(
        app,
        'users',
        method='post',
        data=json.dumps({'usernames': names, 'admin': True}),
    )
    assert r.status_code == 201
    reply = r.json()
    r_names = [user['name'] for user in reply]
    assert names == r_names

    for name in names:
        user = find_user(db, name)
        assert user is not None
        assert user.name == name
        assert user.admin
        assert orm.Role.find(db, 'user') in user.roles
        assert orm.Role.find(db, 'admin') in user.roles


@mark.user
async def test_add_user_bad(app):
    db = app.db
    name = 'dne_newuser'
    r = await api_request(app, 'users', name, method='post')
    assert r.status_code == 400
    user = find_user(db, name)
    assert user is None


@mark.user
async def test_add_user_duplicate(app):
    db = app.db
    name = 'user'
    user = find_user(db, name)
    # double-check that it exists
    assert user is not None
    r = await api_request(app, 'users', name, method='post')
    # special 409 conflict for creating a user that already exists
    assert r.status_code == 409


@mark.user
@mark.role
async def test_add_admin(app):
    db = app.db
    name = 'newadmin'
    r = await api_request(
        app, 'users', name, method='post', data=json.dumps({'admin': True})
    )
    assert r.status_code == 201
    user = find_user(db, name)
    assert user is not None
    assert user.name == name
    assert user.admin
    # assert newadmin has default 'admin' role
    assert orm.Role.find(db, 'user') in user.roles
    assert orm.Role.find(db, 'admin') in user.roles


@mark.user
async def test_delete_user(app):
    db = app.db
    mal = add_user(db, name='mal')
    r = await api_request(app, 'users', 'mal', method='delete')
    assert r.status_code == 204


@mark.user
@mark.role
async def test_make_admin(app):
    db = app.db
    name = 'admin2'
    r = await api_request(app, 'users', name, method='post')
    assert r.status_code == 201
    user = find_user(db, name)
    assert user is not None
    assert user.name == name
    assert not user.admin
    assert orm.Role.find(db, 'user') in user.roles
    assert orm.Role.find(db, 'admin') not in user.roles

    r = await api_request(
        app, 'users', name, method='patch', data=json.dumps({'admin': True})
    )

    assert r.status_code == 200
    user = find_user(db, name)
    assert user is not None
    assert user.name == name
    assert user.admin
    assert orm.Role.find(db, 'user') in user.roles
    assert orm.Role.find(db, 'admin') in user.roles


@mark.user
async def test_set_auth_state(app, auth_state_enabled):
    auth_state = {'secret': 'hello'}
    db = app.db
    name = 'admin'
    user = find_user(db, name, app=app)
    assert user is not None
    assert user.name == name

    r = await api_request(
        app, 'users', name, method='patch', data=json.dumps({'auth_state': auth_state})
    )

    assert r.status_code == 200
    users_auth_state = await user.get_auth_state()
    assert users_auth_state == auth_state


@mark.user
async def test_user_set_auth_state(app, auth_state_enabled):
    auth_state = {'secret': 'hello'}
    db = app.db
    name = 'user'
    user = find_user(db, name, app=app)
    assert user is not None
    assert user.name == name
    user_auth_state = await user.get_auth_state()
    assert user_auth_state is None
    r = await api_request(
        app,
        'users',
        name,
        method='patch',
        data=json.dumps({'auth_state': auth_state}),
        headers=auth_header(app.db, name),
    )
    assert r.status_code == 403
    user_auth_state = await user.get_auth_state()
    assert user_auth_state is None


@mark.user
async def test_admin_get_auth_state(app, auth_state_enabled):
    auth_state = {'secret': 'hello'}
    db = app.db
    name = 'admin'
    user = find_user(db, name, app=app)
    assert user is not None
    assert user.name == name
    await user.save_auth_state(auth_state)

    r = await api_request(app, 'users', name)

    assert r.status_code == 200
    assert r.json()['auth_state'] == auth_state


@mark.user
async def test_user_get_auth_state(app, auth_state_enabled):
    # explicitly check that a user will not get their own auth state via the API
    auth_state = {'secret': 'hello'}
    db = app.db
    name = 'user'
    user = find_user(db, name, app=app)
    assert user is not None
    assert user.name == name
    await user.save_auth_state(auth_state)

    r = await api_request(app, 'users', name, headers=auth_header(app.db, name))

    assert r.status_code == 200
    assert 'auth_state' not in r.json()


async def test_spawn(app):
    db = app.db
    name = 'wash'
    user = add_user(db, app=app, name=name)
    options = {'s': ['value'], 'i': 5}
    before_servers = sorted(db.query(orm.Server), key=lambda s: s.url)
    r = await api_request(
        app, 'users', name, 'server', method='post', data=json.dumps(options)
    )
    assert r.status_code == 201
    assert 'pid' in user.orm_spawners[''].state
    app_user = app.users[name]
    assert app_user.spawner is not None
    spawner = app_user.spawner
    assert app_user.spawner.user_options == options
    assert not app_user.spawner._spawn_pending
    status = await app_user.spawner.poll()
    assert status is None

    assert spawner.server.base_url == ujoin(app.base_url, 'user/%s' % name) + '/'
    url = public_url(app, user)
    kwargs = {}
    if app.internal_ssl:
        kwargs['cert'] = (app.internal_ssl_cert, app.internal_ssl_key)
        kwargs["verify"] = app.internal_ssl_ca
    r = await async_requests.get(url, **kwargs)
    assert r.status_code == 200
    assert r.text == spawner.server.base_url

    r = await async_requests.get(ujoin(url, 'args'), **kwargs)
    assert r.status_code == 200
    argv = r.json()
    assert '--port' not in ' '.join(argv)
    # we pass no CLI args anymore:
    assert len(argv) == 1
    r = await async_requests.get(ujoin(url, 'env'), **kwargs)
    env = r.json()
    for expected in [
        'JUPYTERHUB_USER',
        'JUPYTERHUB_BASE_URL',
        'JUPYTERHUB_API_TOKEN',
        'JUPYTERHUB_SERVICE_URL',
    ]:
        assert expected in env
    if app.subdomain_host:
        assert env['JUPYTERHUB_HOST'] == app.subdomain_host

    r = await api_request(app, 'users', name, 'server', method='delete')
    assert r.status_code == 204

    assert 'pid' not in user.orm_spawners[''].state
    status = await app_user.spawner.poll()
    assert status == 0

    # check that we cleaned up after ourselves
    assert spawner.server is None
    after_servers = sorted(db.query(orm.Server), key=lambda s: s.url)
    assert before_servers == after_servers
    tokens = list(db.query(orm.APIToken).filter(orm.APIToken.user_id == user.id))
    assert tokens == []
    assert app.users.count_active_users()['pending'] == 0


async def test_user_options(app, username):
    db = app.db
    name = username
    user = add_user(db, app=app, name=name)
    options = {'s': ['value'], 'i': 5}
    before_servers = sorted(db.query(orm.Server), key=lambda s: s.url)
    r = await api_request(
        app, 'users', name, 'server', method='post', data=json.dumps(options)
    )
    assert r.status_code == 201
    assert 'pid' in user.orm_spawners[''].state
    app_user = app.users[name]
    assert app_user.spawner is not None
    spawner = app_user.spawner
    assert spawner.user_options == options
    assert spawner.orm_spawner.user_options == options

    # stop the server
    r = await api_request(app, 'users', name, 'server', method='delete')

    # orm_spawner still exists and has a reference to the user_options
    assert spawner.orm_spawner.user_options == options

    # spawn again, no options specified
    # should re-use options from last spawn
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 201
    assert 'pid' in user.orm_spawners[''].state
    app_user = app.users[name]
    assert app_user.spawner is not None
    spawner = app_user.spawner
    assert spawner.user_options == options

    # stop the server
    r = await api_request(app, 'users', name, 'server', method='delete')

    # spawn again, new options specified
    # should override options from last spawn
    new_options = {'key': 'value'}
    r = await api_request(
        app, 'users', name, 'server', method='post', data=json.dumps(new_options)
    )
    assert r.status_code == 201
    assert 'pid' in user.orm_spawners[''].state
    app_user = app.users[name]
    assert app_user.spawner is not None
    spawner = app_user.spawner
    assert spawner.user_options == new_options
    # saved in db
    assert spawner.orm_spawner.user_options == new_options


async def test_spawn_handler(app):
    """Test that the requesting Handler is passed to Spawner.handler"""
    db = app.db
    name = 'salmon'
    user = add_user(db, app=app, name=name)
    app_user = app.users[name]

    # spawn via API with ?foo=bar
    r = await api_request(
        app, 'users', name, 'server', method='post', params={'foo': 'bar'}
    )
    r.raise_for_status()

    # verify that request params got passed down
    # implemented in MockSpawner
    kwargs = {}
    if app.external_certs:
        kwargs['verify'] = app.external_certs['files']['ca']
    url = public_url(app, user)
    r = await async_requests.get(ujoin(url, 'env'), **kwargs)
    env = r.json()
    assert 'HANDLER_ARGS' in env
    assert env['HANDLER_ARGS'] == 'foo=bar'
    # make user spawner.handler doesn't persist after spawn finishes
    assert app_user.spawner.handler is None

    r = await api_request(app, 'users', name, 'server', method='delete')
    r.raise_for_status()


@mark.slow
async def test_slow_spawn(app, no_patience, slow_spawn):
    db = app.db
    name = 'zoe'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    assert r.status_code == 202
    assert app_user.spawner is not None
    assert app_user.spawner._spawn_pending
    assert not app_user.spawner._stop_pending
    assert app.users.count_active_users()['pending'] == 1

    async def wait_spawn():
        while not app_user.running:
            await asyncio.sleep(0.1)

    await wait_spawn()
    assert not app_user.spawner._spawn_pending
    status = await app_user.spawner.poll()
    assert status is None

    async def wait_stop():
        while app_user.spawner._stop_pending:
            await asyncio.sleep(0.1)

    r = await api_request(app, 'users', name, 'server', method='delete')
    r.raise_for_status()
    assert r.status_code == 202
    assert app_user.spawner is not None
    assert app_user.spawner._stop_pending

    r = await api_request(app, 'users', name, 'server', method='delete')
    r.raise_for_status()
    assert r.status_code == 202
    assert app_user.spawner is not None
    assert app_user.spawner._stop_pending

    await wait_stop()
    assert not app_user.spawner._stop_pending
    assert app_user.spawner is not None
    r = await api_request(app, 'users', name, 'server', method='delete')
    # 204 deleted if there's no such server
    assert r.status_code == 204
    assert app.users.count_active_users()['pending'] == 0
    assert app.users.count_active_users()['active'] == 0


async def test_never_spawn(app, no_patience, never_spawn):
    db = app.db
    name = 'badger'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    assert app_user.spawner is not None
    assert app_user.spawner._spawn_pending
    assert app.users.count_active_users()['pending'] == 1

    while app_user.spawner.pending:
        await asyncio.sleep(0.1)
        print(app_user.spawner.pending)

    assert not app_user.spawner._spawn_pending
    status = await app_user.spawner.poll()
    assert status is not None
    # failed spawn should decrement pending count
    assert app.users.count_active_users()['pending'] == 0


async def test_bad_spawn(app, bad_spawn):
    db = app.db
    name = 'prim'
    user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    # check that we don't re-use spawners that failed
    user.spawners[''].reused = True
    assert r.status_code == 500
    assert app.users.count_active_users()['pending'] == 0

    r = await api_request(app, 'users', name, 'server', method='post')
    # check that we don't re-use spawners that failed
    spawner = user.spawners['']
    assert not getattr(spawner, 'reused', False)


async def test_spawn_nosuch_user(app):
    r = await api_request(app, 'users', "nosuchuser", 'server', method='post')
    assert r.status_code == 404


async def test_slow_bad_spawn(app, no_patience, slow_bad_spawn):
    db = app.db
    name = 'zaphod'
    user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    while user.spawner.pending:
        await asyncio.sleep(0.1)
    # spawn failed
    assert not user.running
    assert app.users.count_active_users()['pending'] == 0


def next_event(it):
    """read an event from an eventstream"""
    while True:
        try:
            line = next(it)
        except StopIteration:
            return
        if line.startswith('data:'):
            return json.loads(line.split(':', 1)[1])


@mark.slow
async def test_progress(request, app, no_patience, slow_spawn):
    db = app.db
    name = 'martin'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    assert r.headers['content-type'] == 'text/event-stream'

    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt == {'progress': 0, 'message': 'Server requested'}
    evt = await ex.submit(next_event, line_iter)
    assert evt == {'progress': 50, 'message': 'Spawning server...'}
    evt = await ex.submit(next_event, line_iter)
    url = app_user.url
    assert evt == {
        'progress': 100,
        'message': f'Server ready at {url}',
        'html_message': 'Server ready at <a href="{0}">{0}</a>'.format(url),
        'url': url,
        'ready': True,
    }


async def test_progress_not_started(request, app):
    db = app.db
    name = 'nope'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server', method='delete')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server/progress')
    assert r.status_code == 404


async def test_progress_not_found(request, app):
    db = app.db
    name = 'noserver'
    r = await api_request(app, 'users', 'nosuchuser', 'server/progress')
    assert r.status_code == 404
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server/progress')
    assert r.status_code == 404


async def test_progress_ready(request, app):
    """Test progress API when spawner is already started

    e.g. a race between requesting progress and progress already being complete
    """
    db = app.db
    name = 'saga'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    assert r.headers['content-type'] == 'text/event-stream'
    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 100
    assert evt['ready']
    assert evt['url'] == app_user.url


async def test_progress_bad(request, app, bad_spawn):
    """Test progress API when spawner has already failed"""
    db = app.db
    name = 'simon'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 500
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    assert r.headers['content-type'] == 'text/event-stream'
    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt == {
        'progress': 100,
        'failed': True,
        'message': "Spawn failed: I don't work!",
    }


async def test_progress_bad_slow(request, app, no_patience, slow_bad_spawn):
    """Test progress API when spawner fails while watching"""
    db = app.db
    name = 'eugene'
    app_user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 202
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    assert r.headers['content-type'] == 'text/event-stream'
    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 0
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 50
    evt = await ex.submit(next_event, line_iter)
    assert evt == {
        'progress': 100,
        'failed': True,
        'message': "Spawn failed: I don't work!",
    }


async def progress_forever():
    """progress function that yields messages forever"""
    for i in range(1, 10):
        yield {'progress': i, 'message': 'Stage %s' % i}
        # wait a long time before the next event
        await asyncio.sleep(10)


async def test_spawn_progress_cutoff(request, app, no_patience, slow_spawn):
    """Progress events stop when Spawner finishes

    even if progress iterator is still going.
    """
    db = app.db
    name = 'geddy'
    app_user = add_user(db, app=app, name=name)
    app_user.spawner.progress = progress_forever
    app_user.spawner.delay = 1

    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    r = await api_request(app, 'users', name, 'server/progress', stream=True)
    r.raise_for_status()
    request.addfinalizer(r.close)
    ex = async_requests.executor
    line_iter = iter(r.iter_lines(decode_unicode=True))
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 0
    evt = await ex.submit(next_event, line_iter)
    assert evt == {'progress': 1, 'message': 'Stage 1'}
    evt = await ex.submit(next_event, line_iter)
    assert evt['progress'] == 100


async def test_spawn_limit(app, no_patience, slow_spawn, request):
    db = app.db
    p = mock.patch.dict(app.tornado_settings, {'concurrent_spawn_limit': 2})
    p.start()
    request.addfinalizer(p.stop)

    # start two pending spawns
    names = ['ykka', 'hjarka']
    users = [add_user(db, app=app, name=name) for name in names]
    users[0].spawner._start_future = asyncio.Future()
    users[1].spawner._start_future = asyncio.Future()
    for name in names:
        await api_request(app, 'users', name, 'server', method='post')
    assert app.users.count_active_users()['pending'] == 2

    # ykka and hjarka's spawns are both pending. Essun should fail with 429
    name = 'essun'
    user = add_user(db, app=app, name=name)
    user.spawner._start_future = asyncio.Future()
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 429

    # allow ykka to start
    users[0].spawner._start_future.set_result(None)
    # wait for ykka to finish
    while not users[0].running:
        await asyncio.sleep(0.1)

    assert app.users.count_active_users()['pending'] == 1
    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    assert app.users.count_active_users()['pending'] == 2
    users.append(user)
    # allow hjarka and essun to finish starting
    for user in users[1:]:
        user.spawner._start_future.set_result(None)
    while not all(u.running for u in users):
        await asyncio.sleep(0.1)

    # everybody's running, pending count should be back to 0
    assert app.users.count_active_users()['pending'] == 0
    for u in users:
        u.spawner.delay = 0
        r = await api_request(app, 'users', u.name, 'server', method='delete')
        r.raise_for_status()
    while any(u.spawner.active for u in users):
        await asyncio.sleep(0.1)


@mark.slow
async def test_active_server_limit(app, request):
    db = app.db
    p = mock.patch.dict(app.tornado_settings, {'active_server_limit': 2})
    p.start()
    request.addfinalizer(p.stop)

    # start two pending spawns
    names = ['ykka', 'hjarka']
    users = [add_user(db, app=app, name=name) for name in names]
    for name in names:
        r = await api_request(app, 'users', name, 'server', method='post')
        r.raise_for_status()
    counts = app.users.count_active_users()
    assert counts['active'] == 2
    assert counts['ready'] == 2
    assert counts['pending'] == 0

    # ykka and hjarka's servers are running. Essun should fail with 429
    name = 'essun'
    user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 429
    counts = app.users.count_active_users()
    assert counts['active'] == 2
    assert counts['ready'] == 2
    assert counts['pending'] == 0

    # stop one server
    await api_request(app, 'users', names[0], 'server', method='delete')
    counts = app.users.count_active_users()
    assert counts['active'] == 1
    assert counts['ready'] == 1
    assert counts['pending'] == 0

    r = await api_request(app, 'users', name, 'server', method='post')
    r.raise_for_status()
    counts = app.users.count_active_users()
    assert counts['active'] == 2
    assert counts['ready'] == 2
    assert counts['pending'] == 0
    users.append(user)

    # everybody's running, pending count should be back to 0
    assert app.users.count_active_users()['pending'] == 0
    for u in users:
        if not u.spawner.active:
            continue
        r = await api_request(app, 'users', u.name, 'server', method='delete')
        r.raise_for_status()

    counts = app.users.count_active_users()
    assert counts['active'] == 0
    assert counts['ready'] == 0
    assert counts['pending'] == 0


@mark.slow
async def test_start_stop_race(app, no_patience, slow_spawn):
    user = add_user(app.db, app, name='panda')
    spawner = user.spawner
    # start the server
    r = await api_request(app, 'users', user.name, 'server', method='post')
    assert r.status_code == 202
    assert spawner.pending == 'spawn'
    # additional spawns while spawning shouldn't trigger a new spawn
    with mock.patch.object(spawner, 'start') as m:
        r = await api_request(app, 'users', user.name, 'server', method='post')
    assert r.status_code == 202
    assert m.call_count == 0

    # stop while spawning is not okay
    r = await api_request(app, 'users', user.name, 'server', method='delete')
    assert r.status_code == 400
    while not spawner.ready:
        await asyncio.sleep(0.1)

    spawner.delay = 3
    # stop the spawner
    r = await api_request(app, 'users', user.name, 'server', method='delete')
    assert r.status_code == 202
    assert spawner.pending == 'stop'
    # make sure we get past deleting from the proxy
    await asyncio.sleep(1)
    # additional stops while stopping shouldn't trigger a new stop
    with mock.patch.object(spawner, 'stop') as m:
        r = await api_request(app, 'users', user.name, 'server', method='delete')
    assert r.status_code == 202
    assert m.call_count == 0
    # start while stopping is not allowed
    with mock.patch.object(spawner, 'start') as m:
        r = await api_request(app, 'users', user.name, 'server', method='post')
    assert r.status_code == 400

    while spawner.active:
        await asyncio.sleep(0.1)
    # start after stop is okay
    r = await api_request(app, 'users', user.name, 'server', method='post')
    assert r.status_code == 202


async def test_get_proxy(app):
    r = await api_request(app, 'proxy')
    r.raise_for_status()
    reply = r.json()
    assert list(reply.keys()) == [app.hub.routespec]


async def test_cookie(app):
    db = app.db
    name = 'patience'
    user = add_user(db, app=app, name=name)
    r = await api_request(app, 'users', name, 'server', method='post')
    assert r.status_code == 201
    assert 'pid' in user.orm_spawners[''].state
    app_user = app.users[name]

    cookies = await app.login_user(name)
    cookie_name = app.hub.cookie_name
    # cookie jar gives '"cookie-value"', we want 'cookie-value'
    cookie = cookies[cookie_name][1:-1]
    r = await api_request(app, 'authorizations/cookie', cookie_name, "nothintoseehere")
    assert r.status_code == 404

    r = await api_request(
        app, 'authorizations/cookie', cookie_name, quote(cookie, safe='')
    )
    r.raise_for_status()
    reply = r.json()
    assert reply['name'] == name

    # deprecated cookie in body:
    r = await api_request(app, 'authorizations/cookie', cookie_name, data=cookie)
    r.raise_for_status()
    reply = r.json()
    assert reply['name'] == name


def normalize_token(token):
    for key in ('created', 'last_activity'):
        token[key] = normalize_timestamp(token[key])
    return token


async def test_check_token(app):
    name = 'book'
    user = add_user(app.db, app=app, name=name)
    token = user.new_api_token()
    r = await api_request(app, 'authorizations/token', token)
    r.raise_for_status()
    user_model = r.json()
    assert user_model['name'] == name
    r = await api_request(app, 'authorizations/token', 'notauthorized')
    assert r.status_code == 404


@mark.parametrize("headers, status", [({}, 404), ({'Authorization': 'token bad'}, 404)])
async def test_get_new_token_deprecated(app, headers, status):
    # request a new token
    r = await api_request(
        app, 'authorizations', 'token', method='post', headers=headers
    )
    assert r.status_code == status


@mark.parametrize(
    "headers, status, note, expires_in",
    [
        ({}, 201, 'test note', None),
        ({}, 201, '', 100),
        ({'Authorization': 'token bad'}, 403, '', None),
    ],
)
async def test_get_new_token(app, headers, status, note, expires_in):
    options = {}
    if note:
        options['note'] = note
    if expires_in:
        options['expires_in'] = expires_in
    if options:
        body = json.dumps(options)
    else:
        body = ''
    # request a new token
    r = await api_request(
        app, 'users/admin/tokens', method='post', headers=headers, data=body
    )
    assert r.status_code == status
    if status != 201:
        return
    # check the new-token reply
    reply = r.json()
    assert 'token' in reply
    assert reply['user'] == 'admin'
    assert reply['created']
    assert 'last_activity' in reply
    if expires_in:
        assert isinstance(reply['expires_at'], str)
    else:
        assert reply['expires_at'] is None
    if note:
        assert reply['note'] == note
    else:
        assert reply['note'] == 'Requested via api'
    token_id = reply['id']
    initial = normalize_token(reply)
    # pop token for later comparison
    initial.pop('token')

    # check the validity of the new token
    r = await api_request(app, 'users/admin/tokens', token_id)
    r.raise_for_status()
    reply = r.json()
    assert normalize_token(reply) == initial

    # delete the token
    r = await api_request(app, 'users/admin/tokens', token_id, method='delete')
    assert r.status_code == 204
    # verify deletion
    r = await api_request(app, 'users/admin/tokens', token_id)
    assert r.status_code == 404


@mark.parametrize(
    "as_user, for_user, status",
    [
        ('admin', 'other', 201),
        ('admin', 'missing', 403),
        ('user', 'other', 403),
        ('user', 'user', 201),
    ],
)
async def test_token_for_user(app, as_user, for_user, status):
    # ensure both users exist
    u = add_user(app.db, app, name=as_user)
    if for_user != 'missing':
        for_user_obj = add_user(app.db, app, name=for_user)
    data = {'username': for_user}
    headers = {'Authorization': 'token %s' % u.new_api_token()}
    r = await api_request(
        app,
        'users',
        for_user,
        'tokens',
        method='post',
        data=json.dumps(data),
        headers=headers,
    )
    assert r.status_code == status
    reply = r.json()
    if status != 201:
        return
    assert 'token' in reply

    token_id = reply['id']
    r = await api_request(app, 'users', for_user, 'tokens', token_id, headers=headers)
    r.raise_for_status()
    reply = r.json()
    assert reply['user'] == for_user
    if for_user == as_user:
        note = 'Requested via api'
    else:
        note = 'Requested via api by user %s' % as_user
    assert reply['note'] == note

    # delete the token
    r = await api_request(
        app, 'users', for_user, 'tokens', token_id, method='delete', headers=headers
    )

    assert r.status_code == 204
    r = await api_request(app, 'users', for_user, 'tokens', token_id, headers=headers)
    assert r.status_code == 404


async def test_token_authenticator_noauth(app):
    """Create a token for a user relying on Authenticator.authenticate and no auth header"""
    name = 'user'
    data = {'auth': {'username': name, 'password': name}}
    r = await api_request(
        app,
        'users',
        name,
        'tokens',
        method='post',
        data=json.dumps(data) if data else None,
        noauth=True,
    )
    assert r.status_code == 201
    reply = r.json()
    assert 'token' in reply
    r = await api_request(app, 'authorizations', 'token', reply['token'])
    r.raise_for_status()
    reply = r.json()
    assert reply['name'] == name


async def test_token_authenticator_dict_noauth(app):
    """Create a token for a user relying on Authenticator.authenticate and no auth header"""
    app.authenticator.auth_state = {'who': 'cares'}
    name = 'user'
    data = {'auth': {'username': name, 'password': name}}
    r = await api_request(
        app,
        'users',
        name,
        'tokens',
        method='post',
        data=json.dumps(data) if data else None,
        noauth=True,
    )
    assert r.status_code == 201
    reply = r.json()
    assert 'token' in reply
    r = await api_request(app, 'authorizations', 'token', reply['token'])
    r.raise_for_status()
    reply = r.json()
    assert reply['name'] == name


@mark.parametrize(
    "as_user, for_user, status",
    [
        ('admin', 'other', 200),
        ('admin', 'missing', 404),
        ('user', 'other', 404),
        ('user', 'user', 200),
    ],
)
async def test_token_list(app, as_user, for_user, status):
    u = add_user(app.db, app, name=as_user)
    if for_user != 'missing':
        for_user_obj = add_user(app.db, app, name=for_user)
    headers = {'Authorization': 'token %s' % u.new_api_token()}
    r = await api_request(app, 'users', for_user, 'tokens', headers=headers)
    assert r.status_code == status
    if status != 200:
        return
    reply = r.json()
    assert sorted(reply) == ['api_tokens']
    assert len(reply['api_tokens']) == len(for_user_obj.api_tokens)
    assert all(token['user'] == for_user for token in reply['api_tokens'])
    # validate individual token ids
    for token in reply['api_tokens']:
        r = await api_request(
            app, 'users', for_user, 'tokens', token['id'], headers=headers
        )
        r.raise_for_status()
        reply = r.json()
        assert normalize_token(reply) == normalize_token(token)


# ---------------
# Group API tests
# ---------------


@mark.group
async def test_groups_list(app):
    r = await api_request(app, 'groups')
    r.raise_for_status()
    reply = r.json()
    assert reply == []

    # create two groups
    group = orm.Group(name='alphaflight')
    group_2 = orm.Group(name='betaflight')
    app.db.add(group)
    app.db.add(group_2)
    app.db.commit()

    r = await api_request(app, 'groups')
    r.raise_for_status()
    reply = r.json()
    assert reply == [
        {'kind': 'group', 'name': 'alphaflight', 'users': [], 'roles': []},
        {'kind': 'group', 'name': 'betaflight', 'users': [], 'roles': []},
    ]

    # Test offset for pagination
    r = await api_request(app, "groups?offset=1")
    r.raise_for_status()
    reply = r.json()
    assert r.status_code == 200
    assert reply == [{'kind': 'group', 'name': 'betaflight', 'users': [], 'roles': []}]

    r = await api_request(app, "groups?offset=10")
    r.raise_for_status()
    reply = r.json()
    assert reply == []

    # Test limit for pagination
    r = await api_request(app, "groups?limit=1")
    r.raise_for_status()
    reply = r.json()
    assert r.status_code == 200
    assert reply == [{'kind': 'group', 'name': 'alphaflight', 'users': [], 'roles': []}]

    # 0 is rounded up to 1
    r = await api_request(app, "groups?limit=0")
    r.raise_for_status()
    reply = r.json()
    assert reply == [{'kind': 'group', 'name': 'alphaflight', 'users': [], 'roles': []}]


@mark.group
async def test_add_multi_group(app):
    db = app.db
    names = ['group1', 'group2']
    r = await api_request(
        app, 'groups', method='post', data=json.dumps({'groups': names})
    )
    assert r.status_code == 201
    reply = r.json()
    r_names = [group['name'] for group in reply]
    assert names == r_names

    # try to create the same groups again
    r = await api_request(
        app, 'groups', method='post', data=json.dumps({'groups': names})
    )
    assert r.status_code == 409


@mark.group
async def test_group_get(app):
    group = orm.Group(name='alphaflight')
    app.db.add(group)
    app.db.commit()
    group = orm.Group.find(app.db, name='alphaflight')
    user = add_user(app.db, app=app, name='sasquatch')
    group.users.append(user)
    app.db.commit()

    r = await api_request(app, 'groups/runaways')
    assert r.status_code == 404

    r = await api_request(app, 'groups/alphaflight')
    r.raise_for_status()
    reply = r.json()
    assert reply == {
        'kind': 'group',
        'name': 'alphaflight',
        'users': ['sasquatch'],
        'roles': [],
    }


@mark.group
async def test_group_create_delete(app):
    db = app.db
    user = add_user(app.db, app=app, name='sasquatch')
    r = await api_request(app, 'groups/runaways', method='delete')
    assert r.status_code == 404

    r = await api_request(
        app, 'groups/new', method='post', data=json.dumps({'users': ['doesntexist']})
    )
    assert r.status_code == 400
    assert orm.Group.find(db, name='new') is None

    r = await api_request(
        app,
        'groups/omegaflight',
        method='post',
        data=json.dumps({'users': ['sasquatch']}),
    )
    r.raise_for_status()

    omegaflight = orm.Group.find(db, name='omegaflight')
    sasquatch = find_user(db, name='sasquatch')
    assert omegaflight in sasquatch.groups
    assert sasquatch in omegaflight.users

    # create duplicate raises 400
    r = await api_request(app, 'groups/omegaflight', method='post')
    assert r.status_code == 409

    r = await api_request(app, 'groups/omegaflight', method='delete')
    assert r.status_code == 204
    assert omegaflight not in sasquatch.groups
    assert orm.Group.find(db, name='omegaflight') is None

    # delete nonexistent gives 404
    r = await api_request(app, 'groups/omegaflight', method='delete')
    assert r.status_code == 404


@mark.group
async def test_group_add_delete_users(app):
    db = app.db
    group = orm.Group(name='alphaflight')
    app.db.add(group)
    app.db.commit()
    # must specify users
    r = await api_request(app, 'groups/alphaflight/users', method='post', data='{}')
    assert r.status_code == 400

    names = ['aurora', 'guardian', 'northstar', 'sasquatch', 'shaman', 'snowbird']
    users = [add_user(db, app=app, name=name) for name in names]
    r = await api_request(
        app,
        'groups/alphaflight/users',
        method='post',
        data=json.dumps({'users': names}),
    )
    r.raise_for_status()

    for user in users:
        print(user.name)
        assert [g.name for g in user.groups] == ['alphaflight']

    group = orm.Group.find(db, name='alphaflight')
    assert sorted(u.name for u in group.users) == sorted(names)

    r = await api_request(
        app,
        'groups/alphaflight/users',
        method='delete',
        data=json.dumps({'users': names[:2]}),
    )
    r.raise_for_status()

    for user in users[:2]:
        assert user.groups == []
    for user in users[2:]:
        assert [g.name for g in user.groups] == ['alphaflight']

    group = orm.Group.find(db, name='alphaflight')
    assert sorted(u.name for u in group.users) == sorted(names[2:])


@mark.group
async def test_auth_managed_groups(request, app, group, user):
    group.users.append(user)
    app.db.commit()
    app.authenticator.manage_groups = True
    request.addfinalizer(lambda: setattr(app.authenticator, "manage_groups", False))
    # create groups
    r = await api_request(app, 'groups', method='post')
    assert r.status_code == 400
    r = await api_request(app, 'groups/newgroup', method='post')
    assert r.status_code == 400
    # delete groups
    r = await api_request(app, f'groups/{group.name}', method='delete')
    assert r.status_code == 400
    # add users to group
    r = await api_request(
        app,
        f'groups/{group.name}/users',
        method='post',
        data=json.dumps({"users": [user.name]}),
    )
    assert r.status_code == 400
    # remove users from group
    r = await api_request(
        app,
        f'groups/{group.name}/users',
        method='delete',
        data=json.dumps({"users": [user.name]}),
    )
    assert r.status_code == 400


# -----------------
# Service API tests
# -----------------


@mark.services
async def test_get_services(app, mockservice_url):
    mockservice = mockservice_url
    db = app.db
    r = await api_request(app, 'services')
    r.raise_for_status()
    assert r.status_code == 200

    services = r.json()
    assert services == {
        mockservice.name: {
            'kind': 'service',
            'name': mockservice.name,
            'admin': True,
            'roles': ['admin'],
            'command': mockservice.command,
            'pid': mockservice.proc.pid,
            'prefix': mockservice.server.base_url,
            'url': mockservice.url,
            'info': {},
            'display': True,
        }
    }
    r = await api_request(app, 'services', headers=auth_header(db, 'user'))
    assert r.status_code == 403


@mark.services
async def test_get_service(app, mockservice_url):
    mockservice = mockservice_url
    db = app.db
    r = await api_request(app, 'services/%s' % mockservice.name)
    r.raise_for_status()
    assert r.status_code == 200

    service = r.json()
    assert service == {
        'kind': 'service',
        'name': mockservice.name,
        'admin': True,
        'roles': ['admin'],
        'command': mockservice.command,
        'pid': mockservice.proc.pid,
        'prefix': mockservice.server.base_url,
        'url': mockservice.url,
        'info': {},
        'display': True,
    }
    r = await api_request(
        app,
        'services/%s' % mockservice.name,
        headers={'Authorization': 'token %s' % mockservice.api_token},
    )
    r.raise_for_status()
    r = await api_request(
        app, 'services/%s' % mockservice.name, headers=auth_header(db, 'user')
    )
    assert r.status_code == 403


async def test_root_api(app):
    base_url = app.hub.url
    url = ujoin(base_url, 'api')
    kwargs = {}
    if app.internal_ssl:
        kwargs['cert'] = (app.internal_ssl_cert, app.internal_ssl_key)
        kwargs["verify"] = app.internal_ssl_ca
    r = await api_request(app, bypass_proxy=True)
    r.raise_for_status()
    expected = {'version': jupyterhub.__version__}
    assert r.json() == expected


async def test_info(app):
    r = await api_request(app, 'info')
    r.raise_for_status()
    data = r.json()
    assert data['version'] == jupyterhub.__version__
    assert sorted(data) == [
        'authenticator',
        'python',
        'spawner',
        'sys_executable',
        'version',
    ]
    assert data['python'] == sys.version
    assert data['sys_executable'] == sys.executable
    assert data['authenticator'] == {
        'class': 'jupyterhub.tests.mocking.MockPAMAuthenticator',
        'version': jupyterhub.__version__,
    }
    assert data['spawner'] == {
        'class': 'jupyterhub.tests.mocking.MockSpawner',
        'version': jupyterhub.__version__,
    }


# ------------------
# Activity API tests
# ------------------


async def test_update_activity_403(app, user, admin_user):
    token = user.new_api_token()
    r = await api_request(
        app,
        f"users/{admin_user.name}/activity",
        headers={"Authorization": f"token {token}"},
        data="{}",
        method="post",
    )
    assert r.status_code == 404


async def test_update_activity_admin(app, user, admin_user):
    token = admin_user.new_api_token(roles=['admin'])
    r = await api_request(
        app,
        f"users/{user.name}/activity",
        headers={"Authorization": f"token {token}"},
        data=json.dumps({"last_activity": utcnow().isoformat()}),
        method="post",
    )
    r.raise_for_status()


@mark.parametrize(
    "server_name, fresh",
    [
        ("", True),
        ("", False),
        ("exists", True),
        ("exists", False),
        ("nope", True),
        ("nope", False),
    ],
)
async def test_update_server_activity(app, user, server_name, fresh):
    token = user.new_api_token()
    now = utcnow()
    internal_now = now.replace(tzinfo=None)
    # we use naive utc internally
    # initialize last_activity for one named and the default server
    for name in ("", "exists"):
        user.spawners[name].orm_spawner.last_activity = now.replace(tzinfo=None)
    app.db.commit()

    td = timedelta(minutes=1)
    if fresh:
        activity = now + td
    else:
        activity = now - td

    r = await api_request(
        app,
        f"users/{user.name}/activity",
        headers={"Authorization": f"token {token}"},
        data=json.dumps(
            {"servers": {server_name: {"last_activity": activity.isoformat()}}}
        ),
        method="post",
    )
    if server_name == "nope":
        assert r.status_code == 400
        reply = r.json()
        assert server_name in reply["message"]
        assert "No such server" in reply["message"]
        assert user.name in reply["message"]
        return

    r.raise_for_status()

    # check that last activity was updated

    if fresh:
        expected = activity.replace(tzinfo=None)
    else:
        expected = now.replace(tzinfo=None)

    assert user.spawners[server_name].orm_spawner.last_activity == expected


# -----------------
# General API tests
# -----------------


async def test_options(app):
    r = await api_request(app, 'users', method='options')
    r.raise_for_status()
    assert 'Access-Control-Allow-Headers' in r.headers


async def test_bad_json_body(app):
    r = await api_request(app, 'users', method='post', data='notjson')
    assert r.status_code == 400


# ---------------------------------
# Shutdown MUST always be last test
# ---------------------------------


def test_shutdown(app):
    loop = app.io_loop

    # have to do things a little funky since we are going to stop the loop,
    # which makes gen_test unhappy. So we run the loop ourselves.

    async def shutdown():
        r = await api_request(
            app,
            'shutdown',
            method='post',
            data=json.dumps({'servers': True, 'proxy': True}),
        )
        return r

    real_stop = loop.stop

    def stop():
        stop.called = True
        loop.call_later(1, real_stop)

    with mock.patch.object(loop, 'stop', stop):
        r = loop.run_sync(shutdown, timeout=5)
    r.raise_for_status()
    reply = r.json()
    assert stop.called
