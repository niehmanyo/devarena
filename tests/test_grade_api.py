import json
import pathlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import server

class GradingAPITest(unittest.TestCase):
    def test_grade_and_reject_overflow(self):
        with tempfile.TemporaryDirectory() as directory:
            old_data, old_state=server.DATA,server.STATE
            server.DATA=pathlib.Path(directory)
            server.STATE=server.DATA/"state.json"
            db={"files":[],"attempts":[],
                "exams":[{"id":"e","qs":[{"id":"q","type":"text","score":10}]}],
                "submissions":[{"id":"s","examId":"e","objective":5,
                                "manual":3,"score":8,"graded":True}]}
            server.save(db)
            http=server.ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
            t=threading.Thread(target=http.serve_forever,daemon=True)
            t.start()
            def request(value):
                body=json.dumps({"id":"s","questionScores":{"q":value}}).encode()
                req=urllib.request.Request(
                    "http://127.0.0.1:%d/api/grade"%http.server_port,
                    data=body,headers={"Content-Type":"application/json",
                    "X-Admin-Password":"dbzz123"},method="POST")
                return urllib.request.urlopen(req,timeout=3)
            try:
                with self.assertRaises(urllib.error.HTTPError) as err:
                    request(11)
                self.assertEqual(err.exception.code,400)
                self.assertEqual(server.load()["submissions"][0]["score"],8)
                with request(7) as resp:
                    self.assertEqual(resp.status,200)
                self.assertEqual(server.load()["submissions"][0]["score"],12)
                self.assertEqual(server.load()["submissions"][0]["questionScores"],{"q":7})
            finally:
                http.shutdown()
                http.server_close()
                t.join(3)
                server.DATA,server.STATE=old_data,old_state
