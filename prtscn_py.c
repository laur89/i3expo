#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdio.h>
#include <X11/X.h>
#include <X11/Xutil.h>
// Compile hint: gcc -shared -O3 -fPIC -Wl,-soname,prtscn `pkg-config --cflags --libs python3` -o prtscn.so prtscn.c -lX11

// see https://stackoverflow.com/q/8001923/1803648 for the vararg

void _grab_screen(Display *display, Window root, const int, const int, const int, const int, unsigned char *);
void _grab_screen(Display *display, Window root, const int xx, const int yy, const int W, const int H, /*out*/ unsigned char * data)
{
   XImage *image = XGetImage(display, root, xx, yy, W, H, AllPlanes, ZPixmap);

   unsigned long red_mask   = image->red_mask;
   unsigned long green_mask = image->green_mask;
   unsigned long blue_mask  = image->blue_mask;
   int ii = 0;
   for (int y = 0; y < H; y++) {
       for (int x = 0; x < W; x++) {
         unsigned long pixel = XGetPixel(image, x, y);
         data[ii + 2] = (pixel & blue_mask);        // blue
         data[ii + 1] = (pixel & green_mask) >> 8;  // green
         data[ii]     = (pixel & red_mask) >> 16;   // red
         ii += 3;
      }
   }

   XDestroyImage(image);
}


void _grab_from_img(XImage *image, const int, const int, const int, const int, const unsigned long, const unsigned long, const unsigned long, unsigned char *);
void _grab_from_img(XImage *image, const int xx, const int yy, const int W, const int H, const unsigned long red_mask, const unsigned long green_mask, const unsigned long blue_mask, /*out*/ unsigned char * data)
{
   int ii = 0;
   for (int y = yy; y < H; y++) {
       for (int x = xx; x < W; x++) {
         unsigned long pixel = XGetPixel(image, x, y);
         unsigned char blue  = (pixel & blue_mask);
         unsigned char green = (pixel & green_mask) >> 8;
         unsigned char red   = (pixel & red_mask) >> 16;

         data[ii + 2] = blue;
         data[ii + 1] = green;
         data[ii] = red;
         ii += 3;
      }
   }
}


// TODO: rename to get_screen_into to signify out data is to be provided?
void get_screen(const int, const int, const int, const int, unsigned char *);
void get_screen(const int x, const int y, const int w, const int h, /*out*/ unsigned char * data)
{
   Display *display = XOpenDisplay(NULL);
   Window root = DefaultRootWindow(display);

   _grab_screen(display, root, x, y, w, h, data);

   XDestroyWindow(display, root);
   XCloseDisplay(display);
}


int ParseArguments(long arr[],Py_ssize_t size, PyObject *args) {
    /* Get arbitrary number of positive numbers from Py_Tuple */
    Py_ssize_t i;
    PyObject *temp_p, *temp_p2;

    for (i=0;i<size;i++) {
        temp_p = PyTuple_GetItem(args, i);
        if(temp_p == NULL) {return NULL;}

        /* Check if temp_p is numeric */
        if (PyNumber_Check(temp_p) != 1) {
            PyErr_SetString(PyExc_TypeError,"Non-numeric argument.");
            return NULL;
        }

        /* Convert number to python long and than C unsigned long */
        temp_p2 = PyNumber_Long(temp_p);
        arr[i] = PyLong_AsUnsignedLong(temp_p2);
        Py_DECREF(temp_p2);
    }
    return 1;
}


// no idea why, but grabbing all output images from single *image is slower
static PyObject *get_screens_single_image(PyObject *self, PyObject *args)
{
    Py_ssize_t TupleSize = PyTuple_Size(args);

    if (!TupleSize) {
        if (!PyErr_Occurred())
            PyErr_SetString(PyExc_TypeError,"You must supply at least one argument.");
        return Py_None;
    }

    long *nums = malloc(TupleSize * sizeof(unsigned long));
    if (!(ParseArguments(nums, TupleSize, args))) {
        free(nums);
        return Py_None;
    }

    Display *display = XOpenDisplay(NULL);
    Window root = DefaultRootWindow(display);
    XWindowAttributes attr;
    XGetWindowAttributes(display, root, &attr);
    XImage *image = XGetImage(display, root, 0, 0, attr.width, attr.height, AllPlanes, ZPixmap);
    unsigned long red_mask   = image->red_mask;
    unsigned long green_mask = image->green_mask;
    unsigned long blue_mask  = image->blue_mask;

    PyObject *list_out = PyList_New(TupleSize/4);

    for (int i=0; i < TupleSize; i+=4) {
        int data_size = sizeof(unsigned char) * nums[i+2] * nums[i+3] * 3;  // *3 for R,G,B
        unsigned char *data = (unsigned char *) malloc(data_size);
        _grab_from_img(image, nums[i], nums[i+1], nums[i+2]+nums[i], nums[i+3]+nums[i+1], red_mask, green_mask, blue_mask, data);
        PyObject *result = Py_BuildValue("y#", data, data_size);
        free(data);
        PyList_SET_ITEM(list_out, i/4, result);
    }

    free(nums);
    XDestroyImage(image);
    XDestroyWindow(display, root);
    XCloseDisplay(display);

    return list_out;
}


static PyObject *get_screens(PyObject *self, PyObject *args)
{
    Py_ssize_t TupleSize = PyTuple_Size(args);

    if (!TupleSize) {
        if (!PyErr_Occurred())
            PyErr_SetString(PyExc_TypeError,"You must supply at least one argument.");
        return Py_None;
    }

    long *nums = malloc(TupleSize * sizeof(unsigned long));
    if (!(ParseArguments(nums, TupleSize, args))) {
        free(nums);
        return Py_None;
    }

    Display *display = XOpenDisplay(NULL);
    Window root = DefaultRootWindow(display);
    PyObject *list_out = PyList_New(TupleSize/4);

    for (int i=0; i < TupleSize; i+=4) {
        int data_size = sizeof(unsigned char) * nums[i+2] * nums[i+3] * 3;  // *3 for R,G,B
        unsigned char *data = (unsigned char *) malloc(data_size);
        _grab_screen(display, root, nums[i], nums[i+1], nums[i+2], nums[i+3], data);
        PyObject *result = Py_BuildValue("y#", data, data_size);
        free(data);
        PyList_SET_ITEM(list_out, i/4, result);
    }

    free(nums);
    XDestroyWindow(display, root);
    XCloseDisplay(display);

    return list_out;
}


// from https://github.com/morrolinux/i3expo-ng/blob/main/prtscn.c
static PyObject *getScreenMethod(PyObject *self, PyObject *args) {
   int xx, yy, W, H;
    if (!PyArg_ParseTuple(args, "iiii", &xx, &yy, &W, &H)) {
        PyErr_SetString(PyExc_TypeError, "arguments exception");
        return Py_None;
    }

    int data_size = sizeof(unsigned char) * W * H * 3;
    unsigned char *data = (unsigned char *) malloc(data_size);

    Display *display = XOpenDisplay(NULL);
    Window root = DefaultRootWindow(display);

    _grab_screen(display, root, xx, yy, W, H, data);
    PyObject *result = Py_BuildValue("y#", data, data_size);

    free(data);
    XDestroyWindow(display, root);
    XCloseDisplay(display);

    return result;
}


static PyMethodDef prtscn_methods[] = {
   { "get_screens", get_screens, METH_VARARGS, ""},  // note last arg is docs
   { "getScreen", getScreenMethod, METH_VARARGS, ""},
   // NULL terminate Python looking at the object
   { NULL, NULL, 0, NULL }
};


static struct PyModuleDef prtscn_py = {
    PyModuleDef_HEAD_INIT,
    "prtscn_py",  // name of module
    "",        // module docs, may be null
    -1,        // size of per-interpreter state of the module
    prtscn_methods
};

PyMODINIT_FUNC PyInit_prtscn_py(void) {
   return PyModule_Create(&prtscn_py);
}

