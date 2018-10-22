# This file is part of obs_base.
#
# Developed for the LSST Data Management System.
# This product includes software developed by the LSST Project
# (http://www.lsst.org).
# See the COPYRIGHT file at the top-level directory of this distribution
# for details of code ownership.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
#

import unittest

from astropy.time import Time
import astropy.units as u

from astro_metadata_translator import FitsTranslator, StubTranslator
from lsst.daf.base import DateTime

from lsst.obs.base import MakeRawVisitInfoViaObsInfo


class NewTranslator(FitsTranslator, StubTranslator):
    _trivial_map = {
        "exposure_time": "EXPTIME",
        "exposure_id": "EXP-ID",
    }

    def to_location(self):
        return None

    def to_detector_exposure_id(self):
        return self.to_exposure_id()


class MakeTestableVisitInfo(MakeRawVisitInfoViaObsInfo):
    metadataTranslator = NewTranslator


class TestMakeRawVisitInfoViaObsInfo(unittest.TestCase):

    def setUp(self):
        # Reference values
        self.exposure_time = 6.2*u.s
        self.exposure_id = 54321
        self.datetime_begin = Time("2001-01-02T03:04:05.123456789", format="isot", scale="utc")
        self.datetime_begin.precision = 9
        self.datetime_end = Time("2001-01-02T03:04:07.123456789", format="isot", scale="utc")
        self.datetime_end.precision = 9

        self.header = {
            "DATE-OBS": self.datetime_begin.isot,
            "DATE-END": self.datetime_end.isot,
            "INSTRUME": "Irrelevant",
            "TELESCOP": "LSST",
            "TIMESYS": "UTC",
            "EXPTIME": self.exposure_time,
            "EXP-ID": self.exposure_id,
            "EXTRA1": "an abitrary key and value",
            "EXTRA2": 5,
        }

    def testMakeRawVisitInfoViaObsInfo(self):
        maker = MakeTestableVisitInfo()
        visitInfo = maker(self.header)

        self.assertAlmostEqual(visitInfo.getExposureTime(), self.exposure_time.to_value("s"))
        self.assertEqual(visitInfo.getExposureId(), self.exposure_id)
        self.assertEqual(visitInfo.getDate(), DateTime("2001-01-02T03:04:06.123456789Z", DateTime.UTC))
        self.assertNotIn("EXPTIME", self.header)
        self.assertEqual(len(self.header), 2)


if __name__ == "__main__":
    unittest.main()
