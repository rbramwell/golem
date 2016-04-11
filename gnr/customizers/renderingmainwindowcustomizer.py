import os
import datetime
import time
import logging
from PyQt4 import QtCore
from PyQt4.QtGui import QPixmap, QTreeWidgetItem, QPainter, QColor, QPen, QMessageBox

from golem.task.taskstate import SubtaskStatus
from golem.core.common import get_golem_path

from gnr.ui.dialog import RenderingNewTaskDialog, ShowTaskResourcesDialog

from gnr.renderingdirmanager import get_preview_file
from gnr.renderingtaskstate import RenderingTaskDefinition

from gnr.customizers.gnrmainwindowcustomizer import GNRMainWindowCustomizer
from gnr.customizers.renderingnewtaskdialogcustomizer import RenderingNewTaskDialogCustomizer
from gnr.customizers.showtaskresourcesdialogcustomizer import ShowTaskResourcesDialogCustomizer

from gnr.customizers.memoryhelper import resource_size_to_display, translate_resource_index

logger = logging.getLogger(__name__)

frame_renderers = [u"3ds Max Renderer", u"VRay Standalone", u"Blender"]


def subtasks_priority(sub):
    priority = {
        SubtaskStatus.failure: 5,
        SubtaskStatus.resent: 4,
        SubtaskStatus.finished: 3,
        SubtaskStatus.starting: 2,
        SubtaskStatus.waiting: 1}

    return priority[sub.subtask_status]


def insert_item(root, path_table):
    assert isinstance(root, QTreeWidgetItem)

    if len(path_table) > 0:
        for i in range(root.childCount()):
            if path_table[0] == "{}".format(root.child(i).text(0)):
                insert_item(root.child(i), path_table[1:])
                return

        new_child = QTreeWidgetItem([path_table[0]])
        root.addChild(new_child)
        insert_item(new_child, path_table[1:])


class AbsRenderingMainWindowCustomizer(object):
    def _set_rendering_variables(self):
        self.preview_path = os.path.join(get_golem_path(), "gnr", get_preview_file())
        self.last_preview_path = self.preview_path
        self.slider_previews = {}
        self.gui.ui.frameSlider.setVisible(False)

    def _setup_rendering_connections(self):
        QtCore.QObject.connect(self.gui.ui.frameSlider, QtCore.SIGNAL("valueChanged(int)"), self.__update_slider_preview)
        QtCore.QObject.connect(self.gui.ui.outputFile, QtCore.SIGNAL("mouseReleaseEvent(int, int, QMouseEvent)"),
                               self.__open_output_file)
        QtCore.QObject.connect(self.gui.ui.previewLabel, QtCore.SIGNAL("mouseReleaseEvent(int, int, QMouseEvent)"),
                               self.__pixmap_clicked)
        self.gui.ui.previewLabel.setMouseTracking(True)
        QtCore.QObject.connect(self.gui.ui.previewLabel, QtCore.SIGNAL("mouseMoveEvent(int, int, QMouseEvent)"),
                               self.__mouse_on_pixmap_moved)

    def _setup_advance_task_connections(self):
        self.gui.ui.showResourceButton.clicked.connect(self._show_task_resource_clicked)

    def _set_new_task_dialog_customizer(self):
        self.new_task_dialog_customizer = RenderingNewTaskDialogCustomizer(self.new_task_dialog, self.logic)

    def _set_new_task_dialog(self):
        self.new_task_dialog = RenderingNewTaskDialog(self.gui.window)

    def _set_show_task_resource_dialog(self):
        self.show_task_resources_dialog = ShowTaskResourcesDialog(self.gui.window)
        show_task_resources_dialog_customizer = ShowTaskResourcesDialogCustomizer(self.show_task_resources_dialog, self)

    def update_task_additional_info(self, t):
        from gnr.renderingtaskstate import RenderingTaskState
        assert isinstance(t, RenderingTaskState)

        self.current_task_highlighted = t
        self.__set_time_params(t)

        if not isinstance(t.definition, RenderingTaskDefinition):
            return

        self.__set_renderer_params(t)
        self.__set_pbrt_params(t, is_pbrt=(t.definition.renderer == u"PBRT"))

        if t.definition.renderer in frame_renderers and t.definition.renderer_options.use_frames:
            self.__set_frame_preview(t)
        else:
            self.__set_preview(t)

        self.__update_output_file_color()
        self.current_task_highlighted = t

    def show_task_result(self, task_id):
        t = self.logic.get_task(task_id)
        if t.definition.renderer in frame_renderers and t.definition.renderer_options.use_frames:
            file_ = self.__get_frame_name(t.definition, 0)
        else:
            file_ = t.definition.output_file
        if os.path.isfile(file_):
            self.show_file(file_)
        else:
            msg_box = QMessageBox()
            msg_box.setText("No output file defined.")
            msg_box.exec_()

    def __set_time_params(self, t):
        self.gui.ui.subtaskTimeout.setText("{} minutes".format(int(t.definition.subtask_timeout / 60.0)))
        self.gui.ui.fullTaskTimeout.setText(str(datetime.timedelta(seconds=t.definition.full_task_timeout)))
        if t.task_state.time_started != 0.0:
            lt = time.localtime(t.task_state.time_started)
            time_string = time.strftime("%Y.%m.%d  %H:%M:%S", lt)
            self.gui.ui.timeStarted.setText(time_string)

    def __set_renderer_params(self, t):
        mem, index = resource_size_to_display(t.definition.estimated_memory / 1024)
        self.gui.ui.estimatedMemoryLabel.setText("{} {}".format(mem, translate_resource_index(index)))
        #self.gui.ui.resolution.setText("{} x {}".format(t.definition.resolution[0], t.definition.resolution[1]))
        #self.gui.ui.renderer.setText("{}".format(t.definition.renderer))

    def __set_pbrt_params(self, t, is_pbrt=True):
        if is_pbrt:
            self.gui.ui.algorithmType.setText("{}".format(t.definition.renderer_options.algorithm_type))
            self.gui.ui.pixelFilter.setText("{}".format(t.definition.renderer_options.pixel_filter))
            self.gui.ui.samplesPerPixel.setText("{}".format(t.definition.renderer_options.samples_per_pixel_count))

        # self.gui.ui.algorithmType.setVisible(is_pbrt)
        # self.gui.ui.algorithmTypeLabel.setVisible(is_pbrt)
        # self.gui.ui.pixelFilter.setVisible(is_pbrt)
        # self.gui.ui.pixelFilterLabel.setVisible(is_pbrt)
        # self.gui.ui.samplesPerPixel.setVisible(is_pbrt)
        # self.gui.ui.samplesPerPixelLabel.setVisible(is_pbrt)

    def __set_frame_preview(self, t):
        if "resultPreview" in t.task_state.extra_data:
            self.slider_previews = t.task_state.extra_data["resultPreview"]
        self.gui.ui.frameSlider.setVisible(True)
        self.gui.ui.frameSlider.setRange(1, len(t.definition.renderer_options.frames))
        self.gui.ui.frameSlider.setSingleStep(1)
        self.gui.ui.frameSlider.setPageStep(1)
        self.__update_slider_preview()
        first_frame_namee = self.__get_frame_name(t.definition, 0)
        self.gui.ui.outputFile.setText(u"{}".format(first_frame_namee))

    def __set_preview(self, t):
        self.gui.ui.outputFile.setText(u"{}".format(t.definition.output_file))
        self.gui.ui.frameSlider.setVisible(False)
        if "resultPreview" in t.task_state.extra_data:
            file_path = os.path.abspath(t.task_state.extra_data["resultPreview"])
            time.sleep(0.5)
            if os.path.exists(file_path):
                self.gui.ui.previewLabel.setPixmap(QPixmap(file_path))
                self.last_preview_path = file_path
        else:
            self.gui.ui.previewLabel.setPixmap(QPixmap(self.preview_path))
            self.last_preview_path = self.preview_path

    def __get_frame_name(self, definition, num):
        output_name, ext = os.path.splitext(definition.output_file)
        frame_num = definition.renderer_options.frames[num]
        output_name += str(frame_num).zfill(4)
        return output_name + ext

    def __update_output_file_color(self):
        if os.path.isfile(self.gui.ui.outputFile.text()):
            self.gui.ui.outputFile.setStyleSheet('color: blue')
        else:
            self.gui.ui.outputFile.setStyleSheet('color: black')

    def _show_task_resource_clicked(self):

        if self.current_task_highlighted:
            res = [os.path.abspath(r) for r in self.current_task_highlighted.definition.resources]
            res.sort()
            self._set_show_task_resource_dialog()

            item = QTreeWidgetItem(["Resources"])
            self.show_task_resources_dialog.ui.folderTreeView.insertTopLevelItem(0, item)
            self.show_task_resources_dialog.ui.closeButton.clicked.connect(self.__show_task_res_close_button_clicked)

            for r in res:
                after_split = r.split("\\")
                insert_item(item, after_split)

            self.show_task_resources_dialog.ui.mainSceneFileLabel.setText(
                self.current_task_highlighted.definition.main_scene_file)
            self.show_task_resources_dialog.ui.folderTreeView.expandAll()

            self.show_task_resources_dialog.show()

    def __show_task_res_close_button_clicked(self):
        self.show_task_resources_dialog.window.close()

    def __update_slider_preview(self):
        num = self.gui.ui.frameSlider.value() - 1
        self.gui.ui.outputFile.setText(self.__get_frame_name(self.current_task_highlighted.definition, num))
        self.__update_output_file_color()
        if len(self.slider_previews) > num:
            if self.slider_previews[num]:
                if os.path.exists(self.slider_previews[num]):
                    self.gui.ui.previewLabel.setPixmap(QPixmap(self.slider_previews[num]))
                    self.last_preview_path = self.slider_previews[num]
                    return

        self.gui.ui.previewLabel.setPixmap(QPixmap(self.preview_path))
        self.last_preview_path = self.preview_path

    def __open_output_file(self):
        file_ = self.gui.ui.outputFile.text()
        if os.path.isfile(file_):
            self.show_file(file_)

    def __get_task_num_from_pixels(self, x, y):
        num = None

        t = self.current_task_highlighted
        if t is None or not isinstance(t.definition, RenderingTaskDefinition):
            return

        if t.definition.renderer:
            definition = t.definition
            task_id = definition.task_id
            task = self.logic.get_task(task_id)
            renderer = self.logic.get_renderer(definition.renderer)
            if len(task.task_state.subtask_states) > 0:
                total_tasks = task.task_state.subtask_states.values()[0].extra_data['total_tasks']
                if definition.renderer in frame_renderers and definition.renderer_options.use_frames:
                    frames = len(definition.renderer_options.frames)
                    frame_num = self.gui.ui.frameSlider.value()
                    num = renderer.get_task_num_from_pixels(x, y, total_tasks, use_frames=True, frames=frames,
                                                            frame_num=frame_num)
                else:
                    num = renderer.get_task_num_from_pixels(x, y, total_tasks)
        return num

    def __get_subtask(self, num):
        subtask = None
        task = self.logic.get_task(self.current_task_highlighted.definition.task_id)
        subtasks = [sub for sub in task.task_state.subtask_states.values() if
                    sub.extra_data['start_task'] <= num <= sub.extra_data['end_task']]
        if len(subtasks) > 0:
            subtask = min(subtasks, key=lambda x: subtasks_priority(x))
        return subtask

    def __pixmap_clicked(self, x, y, *args):
        num = self.__get_task_num_from_pixels(x, y)
        if num is not None:
            subtask = self.__get_subtask(num)
            if subtask is not None:
                self.show_subtask_details_dialog(subtask)

    def __mouse_on_pixmap_moved(self, x, y, *args):
        num = self.__get_task_num_from_pixels(x, y)
        if num is not None:
            definition = self.current_task_highlighted.definition
            if not isinstance(definition, RenderingTaskDefinition):
                return
            renderer = self.logic.get_renderer(definition.renderer)
            subtask = self.__get_subtask(num)
            if subtask is not None:
                if definition.renderer in frame_renderers and definition.renderer_options.use_frames:
                    frames = len(definition.renderer_options.frames)
                    frame_num = self.gui.ui.frameSlider.value()
                    border = renderer.get_task_boarder(subtask.extra_data['start_task'],
                                                       subtask.extra_data['end_task'],
                                                       subtask.extra_data['total_tasks'],
                                                       self.current_task_highlighted.definition.resolution[0],
                                                       self.current_task_highlighted.definition.resolution[1],
                                                       use_frames=True,
                                                       frames=frames,
                                                       frame_num=frame_num)
                else:
                    border = renderer.get_task_boarder(subtask.extra_data['start_task'],
                                                       subtask.extra_data['end_task'],
                                                       subtask.extra_data['total_tasks'],
                                                       self.current_task_highlighted.definition.resolution[0],
                                                       self.current_task_highlighted.definition.resolution[1])

                if os.path.isfile(self.last_preview_path):
                    self.__draw_boarder(border)

    def __draw_boarder(self, border):
        pixmap = QPixmap(self.last_preview_path)
        p = QPainter(pixmap)
        pen = QPen(QColor(0, 0, 0))
        pen.setWidth(3)
        p.setPen(pen)
        for (x, y) in border:
            p.drawPoint(x, y)
        p.end()
        self.gui.ui.previewLabel.setPixmap(pixmap)


class RenderingMainWindowCustomizer(AbsRenderingMainWindowCustomizer, GNRMainWindowCustomizer):
    def __init__(self, gui, logic):
        GNRMainWindowCustomizer.__init__(self, gui, logic)
        self._set_rendering_variables()
        self._setup_rendering_connections()
        self._setup_advance_task_connections()