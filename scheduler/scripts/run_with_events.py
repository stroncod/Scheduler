# Copyright (c) 2016-2023 Association of Universities for Research in Astronomy, Inc. (AURA)
# For license information see LICENSE or https://opensource.org/licenses/BSD-3-Clause

from lucupy.minimodel import CloudCover, ImageQuality, Site

from run_main import main

if __name__ == '__main__':
    use_events = True
    main(test_events=True,
         num_nights_to_schedule=3,
         cc_per_site={Site.GS: CloudCover.CC70},
         iq_per_site={Site.GS: ImageQuality.IQ70})
