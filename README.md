<div align="center">
    <img src="/share/jupyterhub/static/images/elixirhub.svg" width=120 alt="logo" />
    <br />
    <br />
    <small>Hub of ElixirNote</small>
</div>

# ElixirHub

With [ElixirHub](https://ciusji.gitbook.io/elixirnote/guides/elixirnote-hub) you can create a
**multi-user Hub** that spawns, manages, and proxies multiple instances of the
single-user [ElixirNote](https://ciusji.gitbook.io/elixirnote/)
server.

[Project ElixirNote](https://github.com/ElixirNote) created ElixirHub to support many
users. The Hub can offer notebook servers to a class of students, a corporate
data science workgroup, a scientific research project, or a high-performance
computing group.


## Screenshot & Gits

### Dashboard

![dashboard](/share/jupyterhub/static/images/dashboard.png)

### Extensions

![extensions](/share/jupyterhub/static/images/extensions.png)

### ElixirNote Launch

![launch](/share/jupyterhub/static/images/launch.png)


## Technical overview

Three main actors make up ElixirHub:

- multi-user **Hub** (tornado process)
- configurable http **proxy** (node-http-proxy)
- multiple **single-user ElixirNote servers** (Python/Jupyter/tornado)

Basic principles for operation are:

- Hub launches a proxy.
- The Proxy forwards all requests to Hub by default.
- Hub handles login and spawns single-user servers on demand.
- Hub configures proxy to forward URL prefixes to the single-user notebook
  servers.


## Installation

### Check prerequisites

- A Linux/Unix based system
- [Python](https://www.python.org/downloads/) 3.6 or greater
- [nodejs/npm](https://www.npmjs.com/)

  - If you are using **`conda`**, the nodejs and npm dependencies will be installed for
    you by conda.

  - If you are using **`pip`**, install a recent version (at least 12.0) of
    [nodejs/npm](https://docs.npmjs.com/getting-started/installing-node).

- If using the default PAM Authenticator, a [pluggable authentication module (PAM)](https://en.wikipedia.org/wiki/Pluggable_authentication_module).
- TLS certificate and key for HTTPS communication
- Domain name

### Install packages

#### Using `pip` locally

ElixirHub can be installed with `pip`, and the proxy with `npm`:

```bash
git clone git@github.com:ElixirNote/elixirhub.git
npm install -g configurable-http-proxy
pip3 install jhub_cas_authenticator
cd jupyterhub
pip3 install .
```
If any problems, please contact bqjimaster@gmail.com.

### Run the Hub server

To start the Hub server, run the command:

    elixirhub

Visit `http://localhost:8000` in your browser, and sign in with your system username and password.

_Note_: To allow multiple users to sign in to the server, you will need to
run the `elixirhub` command as a _privileged user_, such as root.


## Configuration

The [Getting Started](https://ciusji.gitbook.io/elixirnote/guides/elixirnote-hub/get-started) section of the
documentation explains the common steps in setting up ElixirHub.

The [**ElixirHub tutorial**](https://ciusji.gitbook.io/elixirnote/guides/elixirnote-hub/get-started)
provides an in-depth video and sample configurations of ElixirHub.

### Start the Hub

To start the Hub on a specific url and port `10.0.1.2:443` with **https**:

    elixirhub --ip 10.0.1.2 --port 443 --ssl-key my_ssl.key --ssl-cert my_ssl.cert

### Authenticators

| Authenticator                                                                | Description                                       |
| ---------------------------------------------------------------------------- | ------------------------------------------------- |
| PAMAuthenticator                                                             | Built-in authenticator                   |
| [OAuthenticator](https://github.com/jupyterhub/oauthenticator)               | OAuth + JupyterHub Authenticator = OAuthenticator |
| [ldapauthenticator](https://github.com/jupyterhub/ldapauthenticator)         | Simple LDAP Authenticator Plugin for JupyterHub   |
| [kerberosauthenticator](https://github.com/jupyterhub/kerberosauthenticator) | Kerberos Authenticator Plugin for JupyterHub      |

### Spawners

| Spawner                                                        | Description                                                                |
| -------------------------------------------------------------- | -------------------------------------------------------------------------- |
| LocalProcessSpawner                                            | Default, built-in spawner starts single-user servers as local processes    |
| [dockerspawner](https://github.com/jupyterhub/dockerspawner)   | Spawn single-user servers in Docker containers                             |
| [kubespawner](https://github.com/jupyterhub/kubespawner)       | Kubernetes spawner for JupyterHub                                          |
| [sudospawner](https://github.com/jupyterhub/sudospawner)       | Spawn single-user servers without being root                               |
| [systemdspawner](https://github.com/jupyterhub/systemdspawner) | Spawn single-user notebook servers using systemd                           |
| [batchspawner](https://github.com/jupyterhub/batchspawner)     | Designed for clusters using batch scheduling software                      |
| [yarnspawner](https://github.com/jupyterhub/yarnspawner)       | Spawn single-user notebook servers distributed on a Hadoop cluster         |
| [wrapspawner](https://github.com/jupyterhub/wrapspawner)       | WrapSpawner and ProfilesSpawner enabling runtime configuration of spawners |

## Docker

A starter [**docker image for ElixirHub**](https://hub.docker.com/r/jupyterhub/jupyterhub/)
gives a baseline deployment of JupyterHub using Docker.

**Important:** This `ElixirNote/elixirhub` image contains only the Hub itself,
with no configuration. In general, one needs to make a derivative image, with
at least a `elixirhub_config.py` setting up an Authenticator and/or a Spawner.
To run the single-user servers, which may be on the same system as the Hub or
not, ElixirNote version 4 or greater must be installed.

The JupyterHub docker image can be started with the following command:

    docker run -p 8000:8000 -d --name elixirhub ElixirNote/elixirhub elixirhub

This command will create a container named `elixirhub` that you can
**stop and resume** with `docker stop/start`.

The Hub service will be listening on all interfaces at port 8000, which makes
this a good choice for **testing ElixirHub on your desktop or laptop**.

If you want to run docker on a computer that has a public IP then you should
(as in MUST) **secure it with ssl** by adding ssl options to your docker
configuration or by using a ssl enabled proxy.

[Mounting volumes](https://docs.docker.com/engine/admin/volumes/volumes/) will
allow you to **store data outside the docker image (host system) so it will be persistent**, even when you start
a new image.

The command `docker exec -it elixirhub bash` will spawn a root shell in your docker
container. You can **use the root shell to create system users in the container**.
These accounts will be used for authentication in ElixrHub's default configuration.

## Contributing

If you would like to contribute to the project, please read our 
[`CONTRIBUTING.md`](CONTRIBUTING.md). The `CONTRIBUTING.md` file
explains how to set up a development installation, how to run the test suite,
and how to contribute to documentation.


### A note about platform support

ElixirHub is supported on Linux/Unix based systems.

ElixirHub officially **does not** support Windows. You may be able to use
ElixirHub on Windows if you use a Spawner and Authenticator that work on
Windows, but the ElixirHub defaults will not. Bugs reported on Windows will not
be accepted, and the test suite will not run on Windows. Small patches that fix
minor Windows compatibility issues (such as basic installation) **may** be accepted,
however. For Windows-based systems, we would recommend running JupyterHub in a
docker container or Linux VM.

[Additional Reference:](http://www.tornadoweb.org/en/stable/#installation) Tornado's documentation on Windows platform support


## License

We use a shared copyright model that enables all contributors to maintain the
copyright on their contributions.

All code is licensed under the terms of the [revised BSD license](./COPYING.md).


## Help and resources

We encourage you to ask questions and share ideas on the [Jupyter community forum](https://discourse.jupyter.org/).
You can also talk with us on our JupyterHub [Gitter](https://gitter.im/jupyterhub/jupyterhub) channel.

- [Reporting Issues](https://github.com/jupyterhub/jupyterhub/issues)
- [JupyterHub tutorial](https://github.com/jupyterhub/jupyterhub-tutorial)
- [Documentation for JupyterHub](https://jupyterhub.readthedocs.io/en/latest/) | [PDF (latest)](https://media.readthedocs.org/pdf/jupyterhub/latest/jupyterhub.pdf) | [PDF (stable)](https://media.readthedocs.org/pdf/jupyterhub/stable/jupyterhub.pdf)
- [Documentation for JupyterHub's REST API][rest api]
- [Documentation for Project Jupyter](http://jupyter.readthedocs.io/en/latest/index.html) | [PDF](https://media.readthedocs.org/pdf/jupyter/latest/jupyter.pdf)
- [Project Jupyter website](https://jupyter.org)
- [Project Jupyter community](https://jupyter.org/community)

JupyterHub follows the Jupyter [Community Guides](https://jupyter.readthedocs.io/en/latest/community/content-community.html).
