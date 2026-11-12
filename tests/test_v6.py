import copy
import unittest
import server

class CompatibilityTests(unittest.TestCase):
    def test_legacy_json_and_quarterly_rank(self):
        exam={"id":"e1","published":True,"title":"故障排查","category":"C++",
              "qs":[{"id":"q1","type":"text","title":"原因","score":10}]}
        db={"exams":[exam],"files":[],"attempts":[],"submissions":[
            {"examId":"e1","employeeId":"100","name":"测试","score":6,
             "graded":True,"at":"2026-11-01T10:00:00"},
            {"examId":"e1","employeeId":"100","name":"测试","score":8,
             "graded":True,"at":"2026-11-04T10:00:00"}]}
        before=copy.deepcopy(db)
        metrics=server.report_metrics(db)
        self.assertEqual(metrics["quarters"][0]["quarter"],"2026 Q4")
        self.assertEqual(metrics["quarters"][0]["ranking"][0]["points"],14)
        self.assertEqual(metrics["improvements"][0]["change"],20)
        public=server.sanitized(db,False)
        self.assertEqual(public["submissions"],[])
        self.assertEqual(db,before)
    def test_optional_question_fields(self):
        exam={"title":"排查","start":"2026-11-01T09:00",
              "end":"2026-11-01T11:00",
              "qs":[{"id":"q1","type":"text","title":"分析","score":10}]}
        server.good_exam(exam)
        exam["qs"][0]["tags"]=["GDB","C++"]
        exam["qs"][0]["category"]="Core Dump"
        server.good_exam(exam)
        exam["qs"][0]["tags"]="GDB"
        with self.assertRaises(ValueError):
            server.good_exam(exam)

if __name__=="__main__":
    unittest.main()
