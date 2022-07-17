# JupyterHub onbuild image

If you base a Dockerfile on this image:

    FROM jupyterhub/jupyterhub-onbuild:1.4.0
    ...

then your `jupyterhub_config.py` adjacent to your Dockerfile will be loaded into the image and used by JupyterHub.

This is how the `jupyter/jupyterhub` docker image behaved prior to 0.6.
