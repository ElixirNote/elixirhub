# our user list
c.Authenticator.allowed_users = ['minrk', 'ellisonbg', 'willingc']

service_name = 'shared-notebook'
service_port = 9999
group_name = 'shared'

# ellisonbg and willingc have access to a shared server:

c.JupyterHub.load_groups = {group_name: ['ellisonbg', 'willingc']}

# start the notebook server as a service
c.JupyterHub.services = [
    {
        'name': service_name,
        'url': f'http://127.0.0.1:{service_port}',
        'command': ['jupyterhub-singleuser', '--debug'],
    }
]

# This "role assignment" is what grants members of the group
# access to the service
c.JupyterHub.load_roles = [
    {
        "name": "shared-notebook",
        "groups": [group_name],
        "scopes": [f"access:services!service={service_name}"],
    },
]


# dummy spawner and authenticator for testing, don't actually use these!
c.JupyterHub.authenticator_class = 'dummy'
c.JupyterHub.spawner_class = 'simple'
c.JupyterHub.ip = '127.0.0.1'  # let's just run on localhost while dummy auth is enabled
