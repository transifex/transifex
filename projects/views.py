import os
import pygments
import pygments.lexers
import pygments.formatters

from django.core.urlresolvers import reverse
from django.http import HttpResponseRedirect, HttpResponse, Http404
from django.shortcuts import render_to_response, get_object_or_404
from django.template import RequestContext, loader, Context
from django.views.generic import create_update, list_detail
from django.utils.translation import ugettext_lazy as _
from django.utils.datastructures import MultiValueDictKeyError
from django.contrib.auth.decorators import login_required
from django.contrib.syndication.views import feed

from projects.models import Project, Component
from projects.forms import ProjectForm, ComponentForm, UnitForm
from transifex.log import logger
from actionlog.models import (log_addition, log_change, log_deletion)
from translations.lib.types.pot import FileFilterError
from translations.models import (POFile, POFileLock)
from translations.models import POFile
from languages.models import Language

# Feeds

def slug_feed(request, slug=None, param='', feed_dict=None):
    """
    Override default feed, using custom (including inexistent) slug.
    
    Provides the functionality needed to decouple the Feed's slug from
    the urlconf, so a feed mounted at "^/feed" can exist.
    
    See also http://code.djangoproject.com/ticket/6969.
    """
    if slug:
        url = "%s/%s" % (slug, param)
    else:
        url = param
    return feed(request, url, feed_dict)


# Projects

@login_required
def project_create_update(request, project_slug=None):

    if project_slug:
        project = get_object_or_404(Project, slug=project_slug)
    else:
        project = None

    if request.method == 'POST':
        project_form = ProjectForm(request.POST, instance=project, 
                                   prefix='project') 
        if project_form.is_valid(): 
            project = project_form.save(commit=False)
            project_id = project.id
            project.save()
            project_form.save_m2m()
            if not project_id:
                log_addition(request, project)
            else:
                log_change(request, project, 'This project has been changed.')
            return HttpResponseRedirect(reverse('project_detail',
                                        args=[project.slug]),)
    else:
        project_form = ProjectForm(instance=project, prefix='project')

    return render_to_response('projects/project_form.html', {
        'project_form': project_form,
        'project': project,
    }, context_instance=RequestContext(request))


@login_required
def project_delete(request, project_slug):
    project = get_object_or_404(Project, slug=project_slug)
    if request.method == 'POST':
        import copy
        project_ = copy.copy(project)
        project.delete()
        log_deletion(request, project_, project_.name)
        request.user.message_set.create(
            message=_("The %s was deleted.") % project.name)
        return HttpResponseRedirect(reverse('project_list'))
    else:
        return render_to_response(
            'projects/project_confirm_delete.html', {'project': project,},
            context_instance=RequestContext(request))


# Components

@login_required
def component_create_update(request, project_slug, component_slug=None):
    """
    Create & update components. Handles associated units
    """
    project = get_object_or_404(Project, slug=project_slug)
    if component_slug:
        component = get_object_or_404(Component, slug=component_slug,
                                      project=project)
        unit = component.unit    
    else:
        component = None
        unit = None
    if request.method == 'POST':
        component_form = ComponentForm(project, request.POST,
                                       instance=component, prefix='component')
        unit_form = UnitForm(request.POST, instance=unit, prefix='unit')
        if component_form.is_valid() and unit_form.is_valid():
            component = component_form.save(commit=False)
            unit = unit_form.save(commit=False)            
            unit.name = component.get_full_name()
            unit.save()
            component.unit = unit
            component_id = component.id
            component.save()
            component_form.save_m2m()
            if not component_id:
                log_addition(request, component)
            else:
                log_change(request, component,
                           'This component has been changed.')
            return HttpResponseRedirect(
                reverse('component_detail',
                        args=[project_slug, component.slug]),)
    else:
        component_form = ComponentForm(project, instance=component,
                                       prefix='component')
        unit_form = UnitForm(instance=unit, prefix='unit')
    return render_to_response('projects/component_form.html', {
        'component_form': component_form,
        'unit_form': unit_form,
        'project' : project,
        'component': component,
    }, context_instance=RequestContext(request))


def component_detail(request, project_slug, component_slug):
    component = get_object_or_404(Component, slug=component_slug,
                                  project__slug=project_slug)
    return list_detail.object_detail(
        request,
        queryset = Component.objects.all(),
        object_id=component.id,
        template_object_name = "component",
    )


@login_required
def component_delete(request, project_slug, component_slug):
    component = get_object_or_404(Component, slug=component_slug,
                                  project__slug=project_slug)
    if request.method == 'POST':
        import copy
        component_ = copy.copy(component)
        component.delete()
        request.user.message_set.create(
            message=_("The %s was deleted.") % component.full_name)
        log_deletion(request, component_, component_.name)        
        return HttpResponseRedirect(reverse('project_detail', 
                                     args=(project_slug,)))
    else:
        return render_to_response('projects/component_confirm_delete.html',
                                  {'component': component,},
                                  context_instance=RequestContext(request))


def component_set_stats(request, project_slug, component_slug):
    component = get_object_or_404(Component, slug=component_slug,
                                  project__slug=project_slug)
    logger.debug("Requested stats calc for component %s" % component.full_name)
    # Checkout
    component.prepare_repo()
    # Calculate statistics
    try:
        component.trans.set_stats()
    except FileFilterError:
        logger.debug("File filter does not allow POTFILES.in file name"
                     " for %s component" % component.full_name)
        # TODO: Figure out why gettext is not working here
        request.user.message_set.create(message = (
            "The file filter of this intltool POT-based component does not "
            " seem to allow the POTFILES.in file. Please fix it."))
    return HttpResponseRedirect(reverse('projects.views.component_detail', 
                                args=(project_slug, component_slug,)))


@login_required
def component_clear_cache(request, project_slug, component_slug):
    component = get_object_or_404(Component, slug=component_slug,
                                  project__slug=project_slug)
    component.clear_cache()
    return HttpResponseRedirect(reverse('projects.views.component_detail', 
                                args=(project_slug, component_slug,)))


def component_file(request, project_slug, component_slug, filename, 
                   view=False, isMsgmerged=True):
    component = get_object_or_404(Component, slug=component_slug,
                                  project__slug=project_slug)
    try:
        content = component.trans.get_file_content(filename, isMsgmerged)
    except IOError:
        raise Http404
    fname = "%s.%s" % (component.full_name, os.path.basename(filename))
    logger.debug("Requested raw file %s" % filename)
    if view:
        lexer = pygments.lexers.GettextLexer()
        formatter = pygments.formatters.HtmlFormatter(linenos='inline')
        # TODO: get the actual encoding via polib
        context = Context({'body': pygments.highlight(content.decode('utf8'),
                                                      lexer, formatter),
                           'style': formatter.get_style_defs(),
                           'title': "%s: %s" % (component.full_name,
                                                os.path.basename(filename))})
        content = loader.get_template('poview.html').render(context)
        response = HttpResponse(content, mimetype='text/html; charset=UTF-8')
        attach = ""
    else:
        response = HttpResponse(content, mimetype='text/plain; charset=UTF-8')
        attach = "attachment;"
    response['Content-Disposition'] = '%s filename=%s' % (attach, fname)
    return response

@login_required
def component_submit_file(request, project_slug, component_slug, 
                          filename):

    if request.method == 'POST':

        component = get_object_or_404(Component, slug=component_slug,
                                    project__slug=project_slug)
        postats = get_object_or_404(POFile, filename=filename,
                                    object_id=component.id)

        try:
            # Adding extra field to the instance
            request.FILES['submited_file'].targetfile = postats.filename 
        except MultiValueDictKeyError:
            # TODO: Figure out why gettext is not working here
            request.user.message_set.create(message=("Please select a " 
                               "file from your system to be uploaded."))
            return HttpResponseRedirect(reverse('projects.views.component_detail', 
                                args=(project_slug, component_slug,)))

        logger.debug("Checking out for component %s" % component.full_name)
        # Checkout
        component.prepare_repo()
        
        try:
            lang_name = postats.language.name
            lang_code = postats.language.code
        except AttributeError:
            lang_name = postats.filename
            lang_code = component.trans.guess_language(postats.filename)

        # TODO: put it somewhere else using the settings.py
        msg="Sending translation for %s" % lang_name

        component.submit(request.FILES, msg, request.user)
        try:
            component.submit(request.FILES, msg, request.user)
            # Calculate new stats
            component.trans.set_stats_for_lang(lang_code)
            request.user.message_set.create(message=("File submited " 
                               "successfully: %s" % postats.filename))
        except:
            logger.debug("Error submiting translation file %s"
                         " for %s component: %s" % (postats.filename,
                                               component.full_name, e))
            # TODO: Figure out why gettext is not working here
            request.user.message_set.create(message = (
                "Sorry, an error is causing troubles to send your file."))

    else:
        # TODO: Figure out why gettext is not working here
        request.user.message_set.create(message = (
                "Sorry, but you need to send a POST request."))
    return HttpResponseRedirect(reverse('projects.views.component_detail', 
                                args=(project_slug, component_slug,)))


@login_required
def component_toggle_lock_file(request, project_slug, component_slug,
                               filename):
    component = get_object_or_404(Component, slug=component_slug,
                                  project__slug=project_slug)
    pofile = get_object_or_404(POFile, component=component, filename=filename)

    try:
        lock = POFileLock.objects.get(pofile=pofile)
        if request.user == lock.owner:
            lock.delete()
            request.user.message_set.create(message="Lock removed.")
        else:
            request.user.message_set.create(
                message="Error: Only the owner of a lock can remove it.")
    except POFileLock.DoesNotExist:
        lock = POFileLock.objects.create(pofile=pofile, owner=request.user)
        request.user.message_set.create(
            message="Lock created. Please don't forget to remove it when "
            "you're done.")
    return HttpResponseRedirect(reverse('projects.views.component_detail',
                                        args=(project_slug, component_slug,)))
