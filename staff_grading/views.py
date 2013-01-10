"""
Implements the staff grading views called by the LMS.

General idea: LMS asks for a submission to grade for a course.  Course staff member grades it, submits it back.

Authentication of users must be done by the LMS--this service requires a
login from the LMS to prevent arbitrary clients from connecting, but does not
validate that the passed-in grader_ids correspond to course staff.
"""

import json
import logging
from statsd import statsd

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, Http404
from django.views.decorators.csrf import csrf_exempt

from controller.models import Submission, GraderStatus
from controller import util
from controller import grader_util
import staff_grading_util
from ml_grading import ml_grading_util

log = logging.getLogger(__name__)

_INTERFACE_VERSION = 1


@csrf_exempt
@statsd.timed('open_ended_assessment.grading_controller.staff_grading.views.time',
              tags=['function:get_next_submission'])
@util.error_if_not_logged_in
def get_next_submission(request):
    """
    Supports GET request with the following arguments:
    course_id -- the course for which to return a submission.
    grader_id -- LMS user_id of the requesting user

    Returns json dict with the following keys:

    version: '1'  (number)

    success: bool

    if success:
      'submission_id': a unique identifier for the submission, to be passed
                       back with the grade.

      'submission': the submission, rendered as read-only html for grading

      'rubric': the rubric, also rendered as html.

      'prompt': the question prompt, also rendered as html.

      'message': if there was no submission available, but nothing went wrong,
                there will be a message field.
    else:
      'error': if success is False, will have an error message with more info.
    }
    """

    if request.method != "GET":
        raise Http404

    course_id = request.GET.get('course_id')
    grader_id = request.GET.get('grader_id')
    location = request.GET.get('location')

    log.debug("Getting next submission for instructor grading for course: {0}."
              .format(course_id))


    if not (course_id or location) or not grader_id:
   
        return util._error_response("required_parameter_missing", _INTERFACE_VERSION)

    if location:
        (found, id) = staff_grading_util.get_single_instructor_grading_item_for_location(location)

    # TODO: save the grader id and match it in save_grade to make sure things
    # are consistent.
    if not location:
        (found, id) = staff_grading_util.get_single_instructor_grading_item(course_id)

    if not found:
        return util._success_response({'message': 'No more submissions to grade.'},
                                      _INTERFACE_VERSION)

    try:
        submission = Submission.objects.get(id=int(id))
    except Submission.DoesNotExist:
        log.error("Couldn't find submission %s for instructor grading", id)
        return util._error_response('failed_to_load_submission',
                                    _INTERFACE_VERSION,
                                    data={'submission_id': id})

    #Get error metrics from ml grading, and get into dictionary form to pass down to staff grading view
    success, ml_error_info=ml_grading_util.get_ml_errors(submission.location)
    if success:
        ml_error_message=staff_grading_util.generate_ml_error_message(ml_error_info)
    else:
        ml_error_message=ml_error_info

    ml_error_message="Machine learning error information: " + ml_error_message

    if submission.state != 'C':
        log.error("Instructor grading got a submission (%s) in an invalid state: ",
            id, submission.state)
        return util._error_response('wrong_internal_state',
                                    _INTERFACE_VERSION,
                                    data={'submission_id': id,
                                     'submission_state': submission.state})

    num_graded, num_pending = staff_grading_util.count_submissions_graded_and_pending_instructor(submission.location)

    response = {'submission_id': id,
                'submission': submission.student_response,
                'rubric': submission.rubric,
                'prompt': submission.prompt,
                'max_score': submission.max_score,
                'ml_error_info' : ml_error_message,
                'problem_name' : submission.problem_id,
                'num_graded' : staff_grading_util.finished_submissions_graded_by_instructor(submission.location).count(),
                'num_pending' : staff_grading_util.submissions_pending_for_location(submission.location).count(),
                'min_for_ml' : settings.MIN_TO_USE_ML,
                }

    log.debug("Sending success response back to instructor grading!")
    log.debug("Sub id from get next: {0}".format(submission.id))
    return util._success_response(response, _INTERFACE_VERSION)


@csrf_exempt
@statsd.timed(
    'open_ended_assessment.grading_controller.staff_grading.views.time',
    tags=['function:save_grade'])
@util.error_if_not_logged_in
def save_grade(request):
    """
    Supports POST requests with the following arguments:

    course_id: int
    grader_id: int
    submission_id: int
    score: int
    feedback: string

    Returns json dict with keys

    version: int
    success: bool
    error: string, present if not success
    """
    if request.method != "POST":
        return util._error_response("Request needs to be GET", _INTERFACE_VERSION)

    course_id = request.POST.get('course_id')
    grader_id = request.POST.get('grader_id')
    submission_id = request.POST.get('submission_id')
    score = request.POST.get('score')
    feedback = request.POST.get('feedback')
    skipped = request.POST.get('skipped')=="True"
    complete_rubric_scores = request.GET.get('can_handle_rubric', False)
    rubric_scores = request.GET.get('rubric_scores',[])

    if (# These have to be truthy
        not (course_id and grader_id and submission_id) or
        # These have to be non-None
        score is None or feedback is None):
        return util._error_response("required_parameter_missing", _INTERFACE_VERSION)

    if skipped:
        log.debug(submission_id)
        success, sub=staff_grading_util.set_instructor_grading_item_back_to_ml(submission_id)

        if not success:
            return util._error_response(sub, _INTERFACE_VERSION)

        return util._success_response({}, _INTERFACE_VERSION)

    try:
        score = int(score)
    except ValueError:
        return util._error_response(
            "grade_save_error",
            _INTERFACE_VERSION,
            data={"msg": "Expected integer score.  Got {0}".format(score)})

    d = {'submission_id': submission_id,
         'score': score,
         'feedback': feedback,
         'grader_id': grader_id,
         'grader_type': 'IN',
         # Humans always succeed (if they grade at all)...
         'status': GraderStatus.success,
         # ...and they're always confident too.
         'confidence': 1.0,
         #And they don't make errors
         'errors' : ""}

    success, header = grader_util.create_and_handle_grader_object(d)

    if not success:
        return util._error_response("grade_save_error", _INTERFACE_VERSION,
                                    data={'msg': 'Internal error'})

    return util._success_response({}, _INTERFACE_VERSION)

@csrf_exempt
@util.error_if_not_logged_in
def get_problem_list(request):
    """
    Get the list of problems that need grading in course request.GET['course_id'].

    Returns:
        list of dicts with keys
           'location'
           'problem_name'
           'num_graded' -- number graded
           'num_pending' -- number pending in the queue
           'min_for_ml' -- minimum needed to make ML model
    """

    if request.method!="GET":
        error_message="Request needs to be GET."
        log.error(error_message)
        return util._error_response(error_message, _INTERFACE_VERSION)

    course_id=request.GET.get("course_id")

    if not course_id:
        error_message="Missing needed tag course_id"
        log.error(error_message)
        return util._error_response(error_message, _INTERFACE_VERSION)

    locations_for_course = [x['location'] for x in
                            list(Submission.objects.filter(course_id=course_id).values('location').distinct())]

    if len(locations_for_course)==0:
        error_message="No problems associated with course."
        log.error(error_message)
        return util._error_response(error_message, _INTERFACE_VERSION)

    location_info=[]
    for location in locations_for_course:
        problem_name = Submission.objects.filter(location=location)[0].problem_id
        submissions_pending = staff_grading_util.submissions_pending_for_location(location).count()
        finished_instructor_graded = staff_grading_util.finished_submissions_graded_by_instructor(location).count()

        problem_name_from_location=location.split("://")[1]
        location_dict={
            'location' : location,
            'problem_name' : problem_name_from_location,
            'num_graded' : finished_instructor_graded,
            'num_pending' : submissions_pending,
            'min_for_ml' : settings.MIN_TO_USE_ML,
            }
        location_info.append(location_dict)

    log.debug(location_info)
    return util._success_response({'problem_list' : location_info},
                                  _INTERFACE_VERSION)
