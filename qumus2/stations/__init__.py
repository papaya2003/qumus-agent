"""How the brain reaches the laboratory: one contract v1 station per module.

    stations.yaml        which address serves each module, sim or real (config.py)
    catalog.py           which module runs which job (flow), from describe()
    jobs.py              ContractJobService: the agent tools' JobService over HTTP
    mirror.py            the station's record feed -> the brain's own run store,
                         and the brain's sample/project writes -> put_records
    simview.py           [sim only] bench drawing and fault panel for the web page
    launcher.py          start the local simulated lab (qumus-simlab) when needed

The brain never imports laboratory code and never opens the laboratory's files.
"""
