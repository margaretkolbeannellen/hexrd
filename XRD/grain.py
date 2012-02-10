#! /usr/bin/env python
# DO-NOT-DELETE revisionify.begin() 
#
#   Copyright (c) 2007-2009 Lawrence Livermore National Security,
#   LLC. Produced at the Lawrence Livermore National Laboratory (Nathan
#   Barton <barton22@llnl.gov>) CODE-OCEC-08-104.
#   
#   Please also read the file NOTICES.
#   
#   This file is part of the mdef package (version 0.2) and is
#   free software: you can redistribute it and/or modify it under the
#   terms of the GNU Lesser General Public License as published by the
#   Free Software Foundation, either version 3 of the License, or (at your
#   option) any later version.
#   
#   This program is distributed in the hope that it will be useful, but
#   WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
#   Lesser General Public License for more details.
#   
#   A copy of the GNU Lesser General Public License may be found in the
#   file NOTICES. If this file is missing, see
#   <http://www.gnu.org/licenses/>.
#
# DO-NOT-DELETE revisionify.end()
import copy
import mdef
import valUnits

import numpy as num
import matrixUtils as mUtil
import Rotations as rot
import Symmetry as sym
import crystallography as xtl # latticeParameters, latticeVectors, getFriedelPair

from scipy import optimize
from scipy.linalg import inv, qr, svd

import sys, os
try:
    import XRD
    # sys.path.append(XRD.getPath()[0]) # should not be necessary
except:
    # probably running from XRD directory; put ".." in path just in case
    sys.path.append(os.path.join(os.getcwd(),os.path.pardir))
    import XRD
from XRD.xrdUtils import calculateBiotStrain, makeMeasuredScatteringVectors

# constants
r2d = 180./num.pi
d2r = 1./r2d

class Grain(object):
    """
    A (maybe) indexed grain

    method to fit: centroid, orientation, strain, strain+orientation;
    small and large-strain versions indices into spots a reference to
    spots?  reference lattice parameters -- not planeData in case it
    gets changed with pressure
    
    fitting methods for orientation, stretch, and centroid?
    
    what happens if fit a spot and the fit is bad? what if decide to
    refine the spot into two spots for clear cases of modest overlap?
    does that happen often enough that we need to worry about it?
    should Spots class handle a change in spot numbers: NO can Spot
    fit methods easily be generalized?  Spot should probably barf if
    asked for fit center if multiple peaks found unless an index is
    given for which peak set claimbedBy for spots that are found to be
    bad?  -- yes, and then if another grain wants to claim the spot,
    it can ask the claiming grain to hand over the spot or tell it
    whether there are multiple peaks or whatever

    """
    __dummyIdxSpotM = -999
    __faildIdxSpotM = -888
    __conflIdxSpotM = -666
    __etaMinDflt = None
    __etaMaxDflt = None
    __etaTolDflt = 0.05*d2r
    __omeTolDflt = 0.25*d2r
    __inParmDict = {
        'rMat':None, # num.eye(3),
        'vMat':num.r_[1.,1.,1.,0.,0.,0.],
        'phaseID':None,
        'claimingSpots':True,
        'lineageList':[],
        'etaMin':__etaMinDflt,
        'etaMax':__etaMaxDflt,
        'etaTol':__etaTolDflt,
        'omeTol':__omeTolDflt, 
        'findByPixelDist':None,
        'uncertainties':False,
        'confidence_level':0.95,
        }
    __debugDflt = False
    __fp_dtype = [
        ('idx0', int), 
        ('idx1', int), 
        ('tth', float), 
        ]
    __reflInfo_dtype = [
        ('iRefl', int),
        ('iHKL', int),
        ('hkl', (int, 3)),
        ('predAngles', (float, 3)),
        ('measAngles', (float, 3)), 
        ('diffAngles', (float, 3)),
        ('predQvec', (float, 3)),
        ('measXYO', (float, 3)), 
        ('measAngles_unc', (float, 3)), 
        ]
    def __init__(self, spots, 
                 refineFlags=None,   # default is complicated
                 pVec=None, # [0., 0., 0.]  # not in kwargs because does not hang off of self
                 grainData=None,
                 **kwargs):
        
        kwHasVMat = kwargs.has_key('vMat')
        for parm, val in self.__inParmDict.iteritems():
            if kwargs.has_key(parm):
                val = kwargs.pop(parm)
            self.__setattr__(parm, val)
        if grainData is not None:
            'will not call findMatches'
            findMatchesKWArgs = {}
            if len(kwargs) > 0:
                raise RuntimeError, 'have unparsed keyword arguments with keys: ' + str(kwargs.keys())
        else:
            findMatchesKWArgs = kwargs
        
        # convert units as necessary
        self.omeTol = valUnits.valWithDflt(self.omeTol, self.__omeTolDflt, 'radians')
        self.etaTol = valUnits.valWithDflt(self.etaTol, self.__etaTolDflt, 'radians')
        
        self.detectorGeom  = spots.detectorGeom.makeNew(pVec=pVec)
        self.planeData     = spots.getPlaneData(self.phaseID)
        self.spots         = spots
             
        # process ome range(s)
        self.omeMin        = spots.getOmegaMins()
        self.omeMax        = spots.getOmegaMaxs()
        
        # lattice operators
        self.__latticeOperators = self.planeData.getLatticeOperators()

        self.__latticeParameters           = self.__latticeOperators['dparms']
        self.__reciprocalLatticeParameters = self.__latticeOperators['rparms']

        self.__fMat = self.__latticeOperators['F']
        self.__bMat = self.__latticeOperators['B']
        
        self.__vol  = self.__latticeOperators['vol']
        
        # set refineFlags for fitPrecession on centered grains
        if refineFlags is None:
            self.__refineFlagsFPC = num.array(
                #(  xc,    yc,     D,    xt,    yt,    zt )
                [ True,  True, False, False, False,  True ] + \
                [False for iDP in range(len(self.detectorGeom.getDParamRefineDflt()))]
                )
        else:
            print "living dangerously and manually setting detector refinement flags"
            self.__refineFlagsFPC = refineFlags
        
        self.debug = self.__debugDflt
        
        self.vecs_associated   = False
        self.gI_rIs            = None
        
        if grainData is None:
            if self.rMat is not None:
                reflInfo, fPairs, completeness = self.findMatches(
                    claimingSpots=False, **findMatchesKWArgs
                    )
                self.grainSpots   = reflInfo
                self.friedelPairs = fPairs
                self.completeness = completeness
                if self.claimingSpots:
                    self.claimSpots()
            else:
                self.grainSpots   = None
                self.friedelPairs = None
                self.completeness = None
        else:
            import copy
            assert self.rMat is None,\
                'if specify grainData, do not also specify rMat'
            if grainData.has_key('pVec'):
                assert self.detectorGeom.pVec is None,\
                    'if specify pVec in grainData, do not also specify pVec as an argument'
            if grainData.has_key('pVec'):
                assert not kwHasVMat, \
                    'if specify vMat in grainData, do not also specify vMat as an argument'
            self.setGrainData(grainData)
                
            if self.claimingSpots:
                self.claimSpots()
            

        self.centered = False
            
        self.didRefinement = False
        
        return
    def __repr__(self):
        format = "%20s = %s\n"
        retval = str(self.__class__.__name__)+"(\n"
        thingList = self.__inParmDict.keys()
        thingList += [
            'latticeParameters',
            ('pVec', self.detectorGeom.pVec),
            'completeness',
            ('# spots', len(self.grainSpots)),
            ('# friedel pairs', len(self.friedelPairs)),
            'didRefinement',
            ]
        #
        for thing in thingList:
            if hasattr(thing,'__iter__'):
                thing, val = thing
                retval += format % (thing, str(val))
            else:
                retval += format % (thing, str(eval("self."+thing)))
        nAll, nHit, nConf, nFail, nMiss = self.getSpotCounts()
        retval += format % ('# conflicted spots', nConf)
        retval += format % ('# failed spots', nFail)
        #
        retval += ")"
        return retval
    def getGrainData(self):
        import copy
        grainData = {}
        grainData['rMat'        ] = copy.deepcopy(self.rMat)
        grainData['vMat'        ] = copy.deepcopy(self.vMat)
        grainData['pVec'        ] = copy.deepcopy(self.detectorGeom.pVec)
        grainData['grainSpots'  ] = copy.deepcopy(self.grainSpots)
        grainData['friedelPairs'] = copy.deepcopy(self.friedelPairs)
        grainData['completeness'] = self.completeness
        grainData['omeTol'      ] = self.omeTol
        grainData['etaTol'      ] = self.etaTol
        return grainData
    def setGrainData(self, grainData):
        import copy
        self.rMat         = copy.deepcopy(grainData['rMat'        ])
        self.grainSpots   = copy.deepcopy(grainData['grainSpots'  ])
        self.friedelPairs = copy.deepcopy(grainData['friedelPairs'])
        self.completeness =               grainData['completeness']
        if grainData.has_key('omeTol'):
            self.omeTol =  grainData['omeTol']
        if grainData.has_key('etaTol'):
            self.etaTol =  grainData['etaTol']
        if grainData.has_key('pVec'):
            self.detectorGeom.pVec = grainData['pVec']
        if grainData.has_key('pVec'):
            vMat = grainData['vMat']
            assert len(vMat.shape) == 2,\
                'vMat is not 2D'
            self.__vMat = vMat
        return
    def newGrain(self, newSpots, claimingSpots=False, 
                 lineage=None, phaseID=None, 
                 rMatTransf=None, vMat=None, 
                 omeTol=None, etaTol=None, 
                 **kwargs):
        """
        return a new grain instance without changing self;
        the new instance will use newSpots;
        
        NOTE: claimingSpots is False by default, so if a grain is to be kept, may
        want to call claimSpots() method
        
        phaseID and rMatTransf are useful for twins or phase transformations
        """
        import copy
        
        # get defaults from __inParmDict and self
        inParmDict = {}
        inParmDict.update(self.__inParmDict)
        for key in self.__inParmDict.keys():
            inParmDict[key] = eval("self."+key)
        # newSelf = self.__class__(*args, **inParmDict)

        if  rMatTransf is None and \
                omeTol is None and \
                etaTol is None and \
                vMat   is None and \
                newSpots is self.spots:
            """
            assume want to transfer data grain and avoid the work of 
            an extra call to findMatches
            """
            grainData = self.getGrainData()
            "leave inParmDict['rMat'] = None"
        else:
            grainData = None
            if rMatTransf is None:
                assert phaseID is None,\
                    'do not specify phaseID without rMatTransf'
                rMat    = copy.deepcopy(self.rMat)
            else:
                # see, for example, hcpVariants and bccVariants 
                # in fe/orientRelations.py 
                rMat    = num.dot(self.rMat, rMatTransf)
            inParmDict['rMat'] = rMat
        
        if not phaseID is None:
            inParmDict['phaseID'] = phaseID
        
        newLineage  = copy.copy(self.lineageList)
        if lineage is not None:
            newLineage.append(lineage)
        inParmDict['lineageList'] = newLineage
        
        inParmDict['pVec'] = copy.deepcopy(self.detectorGeom.pVec)
        if vMat is not None:
            inParmDict['vMat'] = vMat
        else:
            if self.vMat is not None:
                inParmDict['vMat'] = mUtil.symmToVecMV(self.vMat)
        
        inParmDict['claimingSpots'] = claimingSpots # do not default to self.claimingSpots on purpose!
        
        inParmDict['omeTol'] = omeTol or self.omeTol or inParmDict['omeTol']
        inParmDict['etaTol'] = etaTol or self.etaTol or inParmDict['etaTol']

        inParmDict.update(**kwargs)        
        newGrain = self.__class__(newSpots, grainData=grainData, **inParmDict)
        
        return newGrain

    # COM coordinates
    def set_pVec(self, pVec):
        """
        sets pVec properly
        """
        self.detectorGeom.pVec = pVec
        return
    
    # lattice parameters
    def getReferenceLatticeParams(self):
        """
        Return the reference lattice parameters stored on self
        """
        return self.__latticeParameters
    referenceLatticeParameters = property(getReferenceLatticeParams, None, None)
    
    # lattice munging
    def getLatticeParams(self):
        """
        Returns the lattice parameters consistent with stretch tensor
        """
        return xtl.latticeParameters(num.dot(self.uMat, self.__fMat))
    latticeParameters = property(getLatticeParams, None, None)
    
    def getLatticeVectors(self):
        """
        Returns the lattice vector components consistent with the stretch tensor.
        Components are written in the CRYSTAL FRAME.
        """
        return num.dot(self.uMat, self.__fMat)
    fMat = property(getLatticeVectors, None, None)

    def getCellVolume(self):
        """
        Returns the volume of the direct lattice consistent with the stretch tensor.
        """
        Fc = self.fMat
        return num.dot(Fc[:, 0], num.cross(Fc[:, 1], Fc[:, 2]))
    vol = property(getCellVolume, None, None)
    
    def getReciprocalLatticeVectors(self):
        """
        Returns the reciprocal lattice vector components consistent with the stretch tensor.
        components are written in the CRYSTAL FRAME.
        """
        a = self.fMat[:, 0]; b = self.fMat[:, 1]; c = self.fMat[:, 2]
        retval = (1. / self.vol) * num.vstack([num.cross(b, c),
                                               num.cross(c, a),
                                               num.cross(a, b)]).T
        return retval 
    bMat = property(getReciprocalLatticeVectors, None, None)
    
    # stretch tensor munging
    def getStretchTensor(self):
        """
        Returns the components of the left stretch tensor, which is symmetric positive-definite.
        Components are written in the SAMPLE FRAME.  This is the primary representation of
        the stretch tensor in the code base
        """
        return self.__vMat
    def setStretchTensor(self, vVec):
        """
        Sets stretch tensor properly from a 6-vector in the Mandel-Voigt notation.
        
        SEE ALSO: matrixUtils.vecMVToSymm()
        """
        uVec = num.atleast_1d(vVec).flatten()
        assert len(vVec) == 6, 'wrong length U vector'
        
        self.__vMat = mUtil.vecMVToSymm(vVec)
        return
    vMat = property(getStretchTensor, setStretchTensor, None)
    
    def getRightStretchTensor(self):
        """
        Returns the components of the right stretch tensor, which is symmetric positive-definite.
        Components are written in the CRYSTAL FRAME.  The output is calculated as:
        U = R^T * V * R
        This is for convenience and cannot be set independently to preserve self-consistency.
        """
        R = self.rMat
        if R is None:
            R = num.eye(3)
        return num.dot( R.T, num.dot( self.vMat, R ) )
    uMat = property(getRightStretchTensor, None, None)
    
    def getPredAngles(self, validOnly=False, iHKL=None):
        theseSpots = num.ones(len(self.grainSpots), dtype=bool)
        if validOnly:
            theseSpots = theseSpots & (self.grainSpots['iRefl'] >= 0)
        if iHKL is not None:
            theseSpots = theseSpots & (self.grainSpots['iHKL'] == iHKL)
        predAngs = self.grainSpots['predAngles'][theseSpots]
        return predAngs
    
    # special orientation matrices
    def getAlignmentRotation(self):
        """
        num.dot(q, num.eye(3) - 2 * num.diag(num.diag(num.dot(r.T, fMat)) < 0))
        
        """
        rStar, fStar = qr(self.fMat)
        flipMe = num.eye(3) - 2 * num.diag(
            num.diag(
                num.dot(fStar.T, self.fMat)
                ) < 0
            )
        # fix sign flips that might be present in QR factorization
        rStar = num.dot(rStar, flipMe)
        fStar = num.dot(flipMe, fStar)
        return rStar, fStar
    def getReciprocalAlignmentRotation(self):
        """
        """
        tStar, bStar = qr(self.bMat)
        flipMe = num.eye(3) - 2 * num.diag(
            num.diag(
                num.dot(bStar.T, self.bMat)
                ) < 0
            )
        # fix sign flips that might be present in QR factorization
        tStar = num.dot(tStar, flipMe)
        bStar = num.dot(flipMe, bStar)
        return tStar, bStar
    """
    ########################################################
    #          BEGIN HIGHER-ORDER FUNCTIONALITY            #
    ########################################################
    """
    def findMatches(self, 
                    rMat=None, 
                    vMat=None, 
                    strainMag=None, 
                    etaTol=None, 
                    etaMin=None, 
                    etaMax=None,
                    omeTol=None, 
                    omeMin=None, 
                    omeMax=None,
                    findByPixelDist=None,
                    updateSelf=False,
                    claimingSpots=True,
                    testClaims=False,
                    doFit=False,
                    filename=None,
                    ):
        
        from matrixUtils import columnNorm
        import copy
        
        writeOutput=False
        
        if self.uncertainties:
            'need to do fits to have uncertainties'
            doFit = True

        # overwrite rMat, vMat if present
        if rMat is not None:
            self.rMat = rMat
        if vMat is not None:
            self.vMat = vMat
            
        # handle tolerances
        if etaTol is None:
            etaTol = self.etaTol
        if hasattr(etaTol, 'getVal'):
            etaTol = etaTol.getVal('radians')
        if omeTol is None:
            omeTol = self.omeTol
        if hasattr(omeTol, 'getVal'):
            omeTol = omeTol.getVal('radians')
        if findByPixelDist is None:
            findByPixelDist = self.findByPixelDist
        
        'handle eta ranges, if any specified'
        # min
        if etaMin is None:
            etaMin = self.etaMin
        if etaMin is not None:
            if hasattr(etaMin,'__len__'):
                tmp = []
                for i in range(len(etaMin)):
                    if hasattr(etaMin[i], 'getVal'):
                        tmp.append(etaMin[i].getVal('radians'))
                    else:
                        tmp.append(etaMin[i])
                etaMin = tmp
            else:
                if hasattr(etaMin, 'getVal'):
                    etaMin = [etaMin.getVal('radians')]
        # max
        if etaMax is None:
            etaMax = self.etaMax
        if etaMax is not None:    
            if hasattr(etaMax,'__len__'):
                tmp = []
                for i in range(len(etaMax)):
                    if hasattr(etaMax[i], 'getVal'):
                        tmp.append(etaMax[i].getVal('radians'))
                    else:
                        tmp.append(etaMax[i])
                etaMax = tmp
            else:
                if hasattr(etaMax, 'getVal'):
                    etaMax = [etaMax.getVal('radians')]
            # by here, etaMin and etaMax should be lists of angles in radians
            assert len(etaMin) == len(etaMax), \
                   'azimuthal angle ranges are not the same length'
            pass
        
        'handle ome ranges'
        # min
        if omeMin is None:
            omeMin = self.omeMin
        if hasattr(omeMin,'__len__'):
            tmp = []
            for i in range(len(omeMin)):
                if hasattr(omeMin[i], 'getVal'):
                    tmp.append(omeMin[i].getVal('radians'))
                else:
                    tmp.append(omeMin[i])
            omeMin = tmp
        else:
            if hasattr(omeMin, 'getVal'):
                omeMin = [omeMin.getVal('radians')]
        # max
        if omeMax is None:
            omeMax = self.omeMax
        if hasattr(omeMax,'__len__'):
            tmp = []
            for i in range(len(omeMax)):
                if hasattr(omeMax[i], 'getVal'):
                    tmp.append(omeMax[i].getVal('radians'))
                else:
                    tmp.append(omeMax[i])
            omeMax = tmp
        else:
            if hasattr(omeMax, 'getVal'):
                omeMax = [omeMax.getVal('radians')]
        
        assert len(omeMin) == len(omeMax), \
               'oscillation angle ranges are not the same length'
        
        # handle output request
        if filename is not None:
            assert isinstance(filename, str) or isinstance(filename, file), 'Output filename must be a string!'
            writeOutput=True
        
        # make all theoretical scattering vectors
        predQvec, predQAng0, predQAng1 = \
                  self.planeData._PlaneData__makeScatteringVectors(self.rMat, bMat=self.bMat)
        
        # for control of tolerancing
        symHKLs   = self.planeData.getSymHKLs()
        tThRanges = self.planeData.getTThRanges(strainMag=strainMag)
        tThTols   = tThRanges[:, 1] - tThRanges[:, 0]

        # if no pVec, then can use measQAng out of spots
        measQAngAll = None
        if self.detectorGeom.pVec is None:
            measQAngAll = self.spots.getAngCoords()
        
        nPredRefl = 0
        nMeasRefl = 0
        reflInfoList = []
        dummySpotInfo = num.nan * num.ones(3)
        for iHKL, tThTol in enumerate(tThTols):            
            # old # #  work with generated spots for iHKL
            # old # allPredAng = zip(predQAng0[iHKL], predQAng1[iHKL])
            # old # 
            # old # # filter using omega range
            # old # reflInRange0 = num.zeros(allPredAng[2][0].shape, dtype=bool)
            # old # reflInRange1 = num.zeros(allPredAng[2][1].shape, dtype=bool)
            # old # for iOme in range(len(omeMin)):
            # old #     limVecMin = num.r_[num.cos(omeMin[iOme]), num.sin(omeMin[iOme])]
            # old #     limVecMax = num.r_[num.cos(omeMax[iOme]), num.sin(omeMax[iOme])]
            # old #     #
            # old #     wedgeAngle = num.arccos( num.dot( limVecMin, limVecMax ) )
            # old #     #
            # old #     omeVec0 = num.c_[ num.cos(allPredAng[2][0]), num.sin(allPredAng[2][0]) ]
            # old #     omeVec1 = num.c_[ num.cos(allPredAng[2][1]), num.sin(allPredAng[2][1]) ]
            # old #     #
            # old #     dp0 = num.arccos( num.dot(omeVec0, limVecMin) ) + num.arccos( num.dot(omeVec0, limVecMax) )
            # old #     dp1 = num.arccos( num.dot(omeVec1, limVecMin) ) + num.arccos( num.dot(omeVec1, limVecMax) )
            # old #     #
            # old #     reflInRange0 = reflInRange0 | ( abs(dp0 - wedgeAngle) <= num.sqrt(rot.tinyRotAng) )
            # old #     reflInRange1 = reflInRange1 | ( abs(dp1 - wedgeAngle) <= num.sqrt(rot.tinyRotAng) )
            # old # 
            # old # # get culled angle and hkl lists for predicted spots
            # old # culledTTh = num.r_[ allPredAng[0][0][reflInRange0], allPredAng[0][1][reflInRange1] ]
            # old # culledEta = num.r_[ allPredAng[1][0][reflInRange0], allPredAng[1][1][reflInRange1] ]
            # old # culledOme = num.r_[ allPredAng[2][0][reflInRange0], allPredAng[2][1][reflInRange1] ]
                
            # filter using ome ranges
            reflInRange0 = validateQVecAngles(predQAng0[iHKL][2, :], omeMin, omeMax)
            reflInRange1 = validateQVecAngles(predQAng1[iHKL][2, :], omeMin, omeMax)
            
            # DEBUGGING # import pdb;pdb.set_trace()
            
            # now eta (if applicable)
            if etaMin is not None:
                reflInRange0 = num.logical_and( reflInRange0, validateQVecAngles(predQAng0[iHKL][1, :], etaMin, etaMax) )
                reflInRange1 = num.logical_and( reflInRange1, validateQVecAngles(predQAng1[iHKL][1, :], etaMin, etaMax) )

            # get culled angle and hkl lists for predicted spots
            culledTTh = num.r_[ predQAng0[iHKL][0, reflInRange0], predQAng1[iHKL][0, reflInRange1] ]
            culledEta = num.r_[ predQAng0[iHKL][1, reflInRange0], predQAng1[iHKL][1, reflInRange1] ]
            culledOme = num.r_[ predQAng0[iHKL][2, reflInRange0], predQAng1[iHKL][2, reflInRange1] ]
            
            culledHKLs = num.hstack( [ 
                symHKLs[iHKL][:, reflInRange0], 
                symHKLs[iHKL][:, reflInRange1] ] )
            
            culledQvec = num.c_[ predQvec[iHKL][:, reflInRange0], predQvec[iHKL][:, reflInRange1] ]
            
            nThisPredRefl = len(culledTTh)
            
            # DEBUG # print 'nThisPredRefl = '+str(nThisPredRefl)
            
            nPredRefl += nThisPredRefl  # running count of total number of prepdicted reflections
            
            idxSpotM = self.spots.getHKLSpots(iHKL, phaseID=self.phaseID, disallowMasterWhenSplit=True)
            if len(idxSpotM) > 0:
                if measQAngAll is not None:
                    measQAng = measQAngAll[idxSpotM,:]
                else:
                    if self.debug:
                        print 'calling xyoToAng in findMatches, pVec is : '+str(self.detectorGeom.pVec)
                    measXYO  = self.spots.getXYOCoords( idxSpotM )
                    measQAng = self.detectorGeom.xyoToAng( measXYO[:, 0],
                                                           measXYO[:, 1],
                                                           measXYO[:, 2] )
                    measQAng = num.asarray(measQAng).T # sucks, but need it as an array with cols
            else:
                measQAng = num.zeros((0,3))
            
            # loop over culled reflections for this HKL
            for iPredSpot in range(nThisPredRefl):
                thisSpotInfo = [
                    self.__dummyIdxSpotM,                 # spot index
                    iHKL,                                 # HKL index
                    culledHKLs[:, iPredSpot],             # [h, k, l]
                    num.hstack([ culledTTh[iPredSpot],    # [predTTh, predEta, predOme]
                                 culledEta[iPredSpot],    # 
                                 culledOme[iPredSpot] ]), # 
                    dummySpotInfo,                        # [measTTh, measEta, measOme]
                    dummySpotInfo,                        # [diffTTh, diffEta, diffOme]
                    culledQvec[:, iPredSpot],             # predQvec
                    dummySpotInfo,                        # xyoCOM
                    dummySpotInfo,                        # measAng_unc
                    ]
                
                if len(measQAng) > 0:
                    tthDiff = rot.angularDifference( culledTTh[iPredSpot], measQAng[:, 0] )
                    etaDiff = rot.angularDifference( culledEta[iPredSpot], measQAng[:, 1] )
                    omeDiff = rot.angularDifference( culledOme[iPredSpot], measQAng[:, 2] )

                    if findByPixelDist is None:
                        hitRefl = ( tthDiff <= tThTols[iHKL] ) & \
                                  ( etaDiff <= etaTol ) & \
                                  ( omeDiff <= omeTol )
                    else:
                        xPixel, yPixel, oPixel = self.detectorGeom.angToXYO( # need to pass ome in case pVec is set
                            culledTTh[iPredSpot], culledEta[iPredSpot], culledOme[iPredSpot] )
                        hitRefl = self.spots.getPixelIsInSpots( idxSpotM, 
                                                                (xPixel, yPixel, culledOme[iPredSpot] ), 
                                                                pixelDist=findByPixelDist )
                    
                    if hitRefl.any(): # found at least one match
                        nMeasRefl += 1
                        if hitRefl.sum() > 1: # grab the closest
                            # ... use minimum column norm of angular difference vector to choose?
                            angNorm = columnNorm( num.vstack( [ tthDiff, etaDiff, omeDiff ] ) )
                            
                            # for this, just grab first index if there are identical entries
                            bestMatch = num.argmin(angNorm)

                            # contribute to results table
                            thisSpotInfo[0] = num.asscalar(num.where(idxSpotM)[0][bestMatch])
                            thisSpotInfo[4] = measQAng[bestMatch, :].squeeze()
                            thisSpotInfo[5] = num.hstack( [
                                tthDiff[bestMatch],
                                etaDiff[bestMatch],
                                omeDiff[bestMatch] ] )
                            thisSpotInfo[7] = self.spots.getXYOCoords( thisSpotInfo[0] )
                        else: # grab the one guy
                            thisSpotInfo[0] = num.asscalar(num.where(idxSpotM)[0][hitRefl])
                            thisSpotInfo[4] = measQAng[hitRefl, :].squeeze()
                            thisSpotInfo[5] = num.hstack( [
                                tthDiff[hitRefl],
                                etaDiff[hitRefl],
                                omeDiff[hitRefl] ] )
                            thisSpotInfo[7] = self.spots.getXYOCoords( thisSpotInfo[0] )
                        # end if hit multiple
                    else:
                        # ... use minimum column norm of angular difference vector to choose?
                        angNorm = columnNorm( num.vstack( [ tthDiff, etaDiff, omeDiff ] ) )
                        
                        # for this, just grab first index if there are identical entries
                        bestMatch = num.argmin(angNorm)
                        
                        # contribute to results table
                        thisSpotInfo[5] = num.hstack( [
                            tthDiff[bestMatch],
                            etaDiff[bestMatch],
                            omeDiff[bestMatch] ] )
                    # end if hit any
                # end if any measured
                """
                go ahead and fit the spot now that likely to do something with it
                update spot position in case it has changed
                """
                iSpot = thisSpotInfo[0]
                if iSpot >= 0 and doFit:
                    spotIsOkay = True
                    try:
                        if self.uncertainties:
                            angCOM, angCOM_unc = self.spots.fitSpots(iSpot,
                                                                     uncertainties=self.uncertainties, 
                                                                     confidence_level=self.confidence_level)
                            thisSpotInfo[8] = angCOM_unc
                        else:
                            angCOM = self.spots.fitSpots(iSpot)
                    except:
                        print 'fit failed badly, spot is suspect'
                        spotIsOkay = False
                        pass
                    pass
                    if spotIsOkay:
                        """
                        have xyo coords in spots, updated from fit; use those here to get angCOM
                        in case have local pvec
                        """
                        thisSpotInfo[7] = self.spots.getXYOCoords( iSpot )
                        thisSpotInfo[4] = self.detectorGeom.xyoToAng( *thisSpotInfo[7].flatten().tolist() )
                        thisSpotInfo[5] = rot.angularDifference(thisSpotInfo[4], thisSpotInfo[3])
                    else:
                        """mark as bad"""
                        thisSpotInfo[0] = self.__faildIdxSpotM
                        pass
                    pass
                elif self.spots.fitHasFailed(iSpot, subSpotOnly=True):
                    """mark as bad"""
                    thisSpotInfo[0] = self.__faildIdxSpotM
                    pass
                reflInfoList.append(thisSpotInfo)
            # close predicted spot loop for iHKL
        # close loop over HKLs
        
        # output to structured array
        reflInfo = num.array([tuple(i) for i in reflInfoList], dtype=self.__reflInfo_dtype)
        
        # check for conflicts
        boolValidIRefl = reflInfo['iRefl'] >= 0
        reducedIRefl = num.where(reflInfo['iRefl'] >= 0)[0]
        validSpotIdx = reflInfo['iRefl'][reducedIRefl]
        if claimingSpots or testClaims:
            if not claimingSpots:
                conflicts = self.spots.claimSpots(validSpotIdx, self, checkOnly=True)
            else:
                conflicts = self.spots.claimSpots(validSpotIdx, self)
            conflictIRefl = reducedIRefl[conflicts]
            if len(conflictIRefl) > 0:
                if self.debug:
                    print 'in findMatches, %d spots in conflict' % (len(conflictIRefl))
                reflInfo['iRefl'][conflictIRefl] = self.__conflIdxSpotM
        
        ##
        ## grab Friedel pairs here off of hkls
        ##
        ## *) may eventually move FP markings to planeData rather than having to search for them
        ##    here
        #
        # index to valid reflections
        reducedIRefl = num.where(reflInfo['iRefl'] >= 0)[0]
        validSpotID  = reflInfo['iRefl'][reducedIRefl]
        zTol = 1e-12
        if len(reducedIRefl) > 0:
            # 'parent' angles
            pTTh = reflInfo['predAngles'][:, 0]
            pEta = reflInfo['predAngles'][:, 1]
            pOme = reflInfo['predAngles'][:, 2]
            
            # 'daughter' angles
            dOme, dEta = xtl.getFriedelPair(pTTh[reducedIRefl],
                                            pEta[reducedIRefl],
                                            pOme[reducedIRefl], units='radians')
            
            # lump into arrays of [eta, ome] vectors
            #   - must make deepcoopy of slice into reflInfo, cuz we're gonna chop it
            fpAngles0 = copy.deepcopy(reflInfo['predAngles'][reducedIRefl, 1:]).T
            fpAngles1 = num.c_[dEta.flatten(), dOme.flatten()].T
                        
            # Here's the idea:
            # we pop pairs of the parent and daughter lists (and index list) and
            # accumulate them in fPairs as we find them
            # Pop unpaired reflections as well.
            fpListG = []
            fpListL = []
            iSpotG  = copy.deepcopy(validSpotID)
            iSpotL  = copy.deepcopy(reducedIRefl)
            while len(iSpotG) > 1:
                fpMask    = num.ones(len(iSpotG), dtype='bool') # resize mask
                fpMask[0] = False       # always pop top entry
                
                fp0 = num.tile(fpAngles0[:, 0], (fpAngles1.shape[1], 1)).T
                fpd = rot.angularDifference(fp0, fpAngles1)
                
                # find matching pairs here
                dupl = num.where( abs( num.sum(fpd, axis=0 ) ) < zTol)[0]
                
                if len(dupl) == 1:
                    dID = dupl[0]
                    
                    fpListG.append( [ iSpotG[0], iSpotG[dID] ] )
                    fpListL.append( [ iSpotL[0], iSpotL[dID] ] )
                    
                    fpMask[dID] = False # pop the daughter of the top entry
                elif len(dupl) > 1:
                    raise RuntimeError, "There may be duplicated spots in the list"
                
                # apply masks to our args
                iSpotG    = iSpotG[fpMask]
                iSpotL    = iSpotL[fpMask]
                fpAngles0 = fpAngles0[:, fpMask]
                fpAngles1 = fpAngles1[:, fpMask]
                pass
            
            fPairs = num.empty(len(fpListL), dtype=self.__fp_dtype)
            for iFP, rFP in enumerate(fpListL):
                fPairs[iFP] = ( rFP[0],
                                rFP[1],
                                reflInfo['predAngles'][rFP[0], 0] )
        else:
            fPairs = []
        
        completeness = len(reducedIRefl)/float(nPredRefl)
                
        if updateSelf:
            self.grainSpots   = reflInfo
            self.friedelPairs = fPairs
            self.completeness = completeness
        
        if writeOutput:
            if isinstance(filename, file):
                fid = filename
            elif isinstance(filename, str):
                fid = open(filename, 'w')
            convMeasXYO = num.array([1,1,r2d])
            print >> fid, '#\tspotID\thklID' + \
                          '\tH \tK \tL ' + \
                          '\tpredTTh \tpredEta \tpredOme ' + \
                          '\tmeasTTh \tmeasEta \tmeasOme ' + \
                          '\tdiffTTh \tdiffEta \tdiffOme ' + \
                          '\tpredQx  \tpredQy  \tpredQz ' + \
                          '\tmeasX   \tmeasY   \tmeasOme'
            for i in range(len(reflInfo)):
                if reflInfo['iRefl'][i] == self.__dummyIdxSpotM:
                    measAnglesString = '%f       \t%f       \t%f       \t' % tuple(r2d*reflInfo['measAngles'][i])
                    measXYOString    = '%f       \t%f       \t%f' % tuple(reflInfo['measXYO'][i]*convMeasXYO)
                else:
                    measAnglesString = '%1.4e\t%1.4e\t%1.4e\t' % tuple(r2d*reflInfo['measAngles'][i])
                    measXYOString    = '%1.4e\t%1.4e\t%1.4e' % tuple(reflInfo['measXYO'][i]*convMeasXYO)
                
                print >> fid, '\t%d\t' % (reflInfo['iRefl'][i]) + \
                              '%d\t' % (reflInfo['iHKL'][i]) + \
                              '%d\t%d\t%d\t' % tuple(reflInfo['hkl'][i]) + \
                              '%1.4e\t%1.4e\t%1.4e\t' % tuple(r2d*reflInfo['predAngles'][i]) + \
                              measAnglesString + \
                              '%1.4e\t%1.4e\t%1.4e\t' % tuple(r2d*reflInfo['diffAngles'][i]) + \
                              '%1.4e\t%1.4e\t%1.4e\t' % tuple(reflInfo['predQvec'][i]) + \
                              measXYOString
                                                     
        return reflInfo, fPairs, completeness #, (nMeasRefl, nPredRefl)
    
    def getValidSpotIdx(self, ignoreClaims=False):
        masterReflInfo = self.grainSpots
        if ignoreClaims:
            hitReflId      = num.where(masterReflInfo['iRefl'] >= self.__conflIdxSpotM)[0]
        else:
            hitReflId      = num.where(masterReflInfo['iRefl'] >= 0)[0]
        validSpotIdx   = masterReflInfo['iRefl'][hitReflId]
        return validSpotIdx, hitReflId
    def checkClaims(self):
        """
        useful if have not done claims yet and want to check and see if spots 
        are still available;
        updates completeness too
        """
        
        validSpotIdx, hitReflId = self.getValidSpotIdx(ignoreClaims=True)
        conflicts = self.spots.claimSpots(validSpotIdx, self, checkOnly=True)
        conflictIRefl = hitReflId[conflicts]
        if len(conflictIRefl) > 0:
            self.grainSpots['iRefl'][conflictIRefl] = self.__conflIdxSpotM

        nPredRefl = len(self.grainSpots)
        nIRefl    = len(validSpotIdx) - num.sum(num.array(conflicts, dtype=bool))
        self.completeness = float(nIRefl)/float(nPredRefl)
        return
    def getSpotCounts(self):
        masterReflInfo = self.grainSpots
        nAll  = len(masterReflInfo)
        nHit  = num.sum(masterReflInfo['iRefl'] >= 0)
        nConf = num.sum(masterReflInfo['iRefl'] == self.__conflIdxSpotM)
        nFail = num.sum(masterReflInfo['iRefl'] == self.__faildIdxSpotM)
        nMiss = num.sum(masterReflInfo['iRefl'] == self.__dummyIdxSpotM)
        assert nAll == nHit+nConf+nFail+nMiss, \
            'failed sanity check for counts of spot types'
        return (nAll, nHit, nConf, nFail, nMiss)
    def claimSpots(self, asMaster=None):
        """
        claim spots; particularly useful if claimingSpots was False on init;
        assume conflicts are handled elsewhere or ignored if want to claim spots 
        using this method;
        """
        validSpotIdx, hitReflId = self.getValidSpotIdx()
        
        conflicts = self.spots.claimSpots(validSpotIdx, self, checkOnly=False, asMaster=asMaster)
        return
    #
    # FITTING ROUTINES
    #
    def _fitPrecession_objFunc(self, pVec):
        """
        fit precession or alternately do detector calibration
        """
        # make local copy of detector geometry
        tmpDG = self.detectorGeom.makeNew()
        retval = None
        if self.centered:
            # set things in tmpDG
            tmpDG.pVec = None
            
            tmpDG.setupRefinement(self._Grain__refineFlagsFPC)
            tmpDG.updateParams(pVec)    # note that pVec are dg params in this case!

            # grab wavelength
            wlen = self.planeData.wavelength
            
            # grab all valid spots for residual contribution
            validSpots = num.where(self.grainSpots['iRefl'] >= 0)[0]
            for ii, iRow in enumerate(validSpots):
                ## In this case, the predicted angles in self.grainSpots come from
                ## the lattice, which is fixed; altering the detector geometry helps to
                ## bring the predicted and measured angles into coincidence.
                ## 
                ## The 'measXYO' *should* be immutable enough depite the fact the spots
                ## are fit in angular space using the unrefined detector geometry.
                ## 
                ## without uncertainties accounted for, the relatively large omega
                ## uncertainty adversely affects the solution
                measAngs = tmpDG.xyoToAng( *( self.grainSpots['measXYO'][iRow] ) )
                predAngs = self.grainSpots['predAngles'][iRow]
                figOfMerit = ( rot.angularDifference( measAngs[0], predAngs[0] ),
                               rot.angularDifference( measAngs[1], predAngs[1] ) )
                if ii == 0:
                    retval = figOfMerit
                else:
                    retval = num.hstack([retval, figOfMerit])
        else:
            # reset precession vector with current trial
            tmpDG.pVec = pVec
            # first try to  die gracefully if no friedel pairs are attached by
            # artificially 'converging'
            nFP = len(self.friedelPairs)
            if nFP < 3:
                print "Warning: insufficient data for fit!"
                if nFP == 0:
                    retval = num.zeros(3)
                else:
                    retval = num.zeros(3*len(self.friedelPairs))
            else:
                # loop over Friedel pairs only
                for iFP, fPairList in enumerate(self.friedelPairs):
                    if len(fPairList) > 3:
                        # first find + and - vectors
                        # ... need to implement this; should munge indices and continue below
                        raise NotImplementedError
                    else:
                        # grab angles with new detector geom
                        # remember angs are (tTh, eta, ome)
                        fpAng0 = tmpDG.xyoToAng( *( self.grainSpots['measXYO'][fPairList[0]] ) )
                        fpAng1 = tmpDG.xyoToAng( *( self.grainSpots['measXYO'][fPairList[1]] ) )
                        
                        # figure of merit on measured tTh and eta
                        figOfMerit = num.vstack(
                            [ rot.angularDifference(fpAng0[0], fpAng1[0]),
                              rot.angularDifference(fpAng0[1], fpAng1[1]) - num.pi ] )
                        pass
                    if iFP == 0:
                        retval = figOfMerit.T.flatten()
                    else:
                        retval = num.hstack([retval, figOfMerit.T.flatten()])
                        pass
                    pass
                pass
            pass
        if retval is None:
            print "No data to fit!"
            retval = num.zeros(3)
        return retval
    def _fitPrecessionWeighting_objFunc(self, pVec, weighting=False):
        """
        """
        def __fitPrecession_model_func(params, _tempDG, _xyoCOM0, _xyoCOM1):
            """need to encode this sequence in a function in order to propagating uncertainties in
            the spot to get weights for the leastsq solution"""
            # old # xyoCOM_0 = num.array(self.spots.detectorGeom.angToXYO(*params[0:3])).flatten()
            # old # xyoCOM_1 = num.array(self.spots.detectorGeom.angToXYO(*params[3:6])).flatten()
            _fpAng0 = _tempDG.xyoToAng(*_xyoCOM0)
            _fpAng1 = _tempDG.xyoToAng(*_xyoCOM1)
            qxy0 = num.array([num.cos(_fpAng0[1])*num.cos(0.5*_fpAng0[0]),
                              num.sin(_fpAng0[1])*num.cos(0.5*_fpAng0[0])])
            qxy1 = num.array( [num.cos(_fpAng1[1])*num.cos(0.5*_fpAng1[0]),
                               num.sin(_fpAng1[1])*num.cos(0.5*_fpAng1[0])])
            return num.sqrt(num.dot(qxy0+qxy1,qxy0+qxy1))
            
        tmpDG = self.detectorGeom.makeNew()
        if self.centered:
            tmpDG.xc = pVec[0]
            tmpDG.yc = pVec[1]
        else:
            tmpDG.pVec = pVec
        
        retval = []
        ufs = []
        for iFP, fPairList in enumerate(self.friedelPairs):
            if len(fPairList) > 3:
                # first find + and - vectors
                # ... need to implement this; should munge indices and continue below
                raise NotImplementedError
            else:
                # grab angles with new detector geom
                """
                angCOM are potentially calculdated with precessions already, so rely on xyo;
                assume that angCOM do not change enough that uncertainties need to be modified,
                and fitPrecession is probably being done before a pVec was obtained, so that
                xyo are computed without a pVec anyway!
                """
                angCOM_0_unc = self.grainSpots['measAngles_unc'][ fPairList[0] ]
                xyoCOM_0     = self.grainSpots['measXYO'][        fPairList[0] ]
                angCOM_0     = tmpDG.xyoToAng( *( xyoCOM_0 ) )
                #
                angCOM_1_unc = self.grainSpots['measAngles_unc'][ fPairList[1] ]
                xyoCOM_1     = self.grainSpots['measXYO'][        fPairList[1] ]
                angCOM_1     = tmpDG.xyoToAng( *( xyoCOM_1 ) )
                
                mus     = num.hstack(angCOM_0, angCOM_1).flatten()
                mu_uncs = num.hstack(angCOM_0_unc, angCOM_1_unc).flatten()
                extraArgs = (tmpDG, xyoCOM_0, xyoCOM_1) 
                try:
                    import uncertainty_analysis
                    uf = uncertainty_analysis.propagateUncertainty(
                        __fitPrecession_model_func, mu_uncs, 1e-8, mus, *extraArgs)
                    #ufs.append(uf)
                    #print 'uf',uf
                except AssertionError:
                    print 'AssertionError, skipping pair',fPairList[0],fPairList[1]
                    continue
                except IndexError:
                    print 'IndexError, skipping pair',fPairList[0],fPairList[1]
                    continue
            if weighting == False:
                uf = 1.
            ufs.append(1./uf)            
            retval.append(__fitPrecession_model_func(mus, *extraArgs))
            #print retval
            #print ufs
        maxuf = max(ufs)
        print 'maxuf',maxuf,'minuf',min(ufs)
        print'retval',retval
        ufs_ = num.array(ufs)
        #print len(retval),len(ufs_)
        return (num.array(retval)*ufs_)*maxuf   
    def fitPrecession(self, weighting=False, display=True, xtol=1e-12, ftol=1e-12, fout=None):
        """
        Fit the Center-Of-Mass coordinates of the grain in the sample frame
        """
        import sys
        fout = fout or sys.stdout
        
        if self.centered:
            self.detectorGeom.pVec = None # zero out any existing pVec
            pVec0 = self.detectorGeom.getParams(allParams=True)[self._Grain__refineFlagsFPC]
        else:
            if self.detectorGeom.pVec is None:
                pVec0 = num.zeros(3)
            else:
                pVec0 = self.detectorGeom.pVec
        
        if self.uncertainties:
            import uncertainty_analysis
            #the cov_matrix is huge... giving large uncertainty, in progress
            optResults = optimize.leastsq(self._fitPrecessionWeighting_objFunc,
                                          pVec0,
                                          args=weighting,
                                          xtol=xtol,
                                          ftol=ftol,
                                          full_output=1)
            pVec1 = optResults[0]
            cov_x = optResults[1]
            print >> fout, 'cov,solution',cov_x,pVec1
            u_is = uncertainty_analysis.computeAllparameterUncertainties(pVec1, cov_x, confidence_level)
            self.detectorGeom.pVecUncertainties = u_is
            
            pVec1 = optResults[0]
        else:
            optResults = optimize.leastsq(self._fitPrecession_objFunc,
                                          pVec0,
                                          xtol=xtol,
                                          ftol=ftol)
            pVec1 = optResults[0]
            ierr  = optResults[1]

        if self.centered:
            # reset pVec in self
            self.detectorGeom.setupRefinement(self._Grain__refineFlagsFPC)
            self.detectorGeom.updateParams(pVec1)
            self.detectorGeom.setupRefinement([False for iRF in self._Grain__refineFlagsFPC])

            refParams = self.detectorGeom.getParams(allParams=True)
            print >> fout, "refined beam position:\t\t(%g, %g)" % (refParams[0], refParams[1])
            print >> fout, "refined working distance:\t%g" % (refParams[2])
            print >> fout, "refined tilt angles:\t\t(%g, %g, %g)\n" % (refParams[3], refParams[4], refParams[5])

            # we have altered the detector geometry; must update in spots
            print >> fout, "resetting detector geometry in spots"
            "send a copy to resetDetectorGeom as self.detectorGeom may change"
            assert self.detectorGeom.pVec is None,\
                   'something is badly wrong'
            dgForSpots = self.detectorGeom.makeNew()
            self.spots.resetDetectorGeom(dgForSpots)
            self.findMatches(updateSelf=True)
        else:
            if display:
                print >> fout, "refined COM coordinates: (%g, %g, %g)\n" % (pVec1[0], pVec1[1], pVec1[2])
            # reset pVec in self
            self.detectorGeom.pVec = pVec1
        self.didRefinement = True
        return
    def _fitF_objFunc(self, rVec, fitPVec=True):
        """
        Objective function for fitting R and U, the right polar decomposition of the
        deformation gradient F = R * U that takes the reference cell to the current.

        There are 9 degrees of freedom in rVec: rVec[:3] are the exponential map parameters
        of R; rVec[3:] are the 6 components of the biot strain, V - I,  in the Mandel-Voigt
        notation.  The fit is performed on V - I to keep the degrees of freedom on the same
        relative scale.
        """
        # grab relevant data from the results of self.findMatches
        masterReflInfo = self.grainSpots
        hitReflId      = num.where(masterReflInfo['iRefl'] >= 0)[0]
        nRefl          = len(hitReflId)
        

        # wavelength (for normalization)
        wlen = self.planeData.wavelength
                
        # we write the deformation gradient F in terms of its 
        # left polar decomposition: F = U * V
        R = rot.rotMatOfExpMap(num.c_[rVec[:3]])
        V = mUtil.vecMVToSymm(rVec[3:9]) + num.eye(3)
        # U = num.dot(R.T, num.dot(V, R))
        
        if fitPVec:
            p = rVec[9:]
            # make local copy of detector geometry as reset pVec
            tmpDG = self.detectorGeom.makeNew()
            tmpDG.pVec = p
        else:
            tmpDG = self.detectorGeom
        
        # augment reference lattice vectors in place to get their components
        # in the SAMPLE FRAME
        fMat = num.dot(V, num.dot(R, self.__fMat))
        
        # the bMat is by definition the components of the reciprocal lattice
        # vectors written in the crystal frame; we can define it here from the
        # deformed cell in the SAMPLE FRAME
        vol   = num.dot(fMat[:, 0], num.cross(fMat[:, 1], fMat[:, 2]))
        astar = num.cross(fMat[:, 1], fMat[:, 2])
        bstar = num.cross(fMat[:, 2], fMat[:, 0])
        cstar = num.cross(fMat[:, 0], fMat[:, 1])
        bMat  = (1. / vol) * num.vstack([astar, bstar, cstar]).T
        
        # measured data
        #   - measAngs = [tTh, eta, ome]
        #   - Q-vectors (i.e. G-vectors) normalized to have length of 1/d
        measHKLs = masterReflInfo['hkl'][hitReflId, :]
        measXYO  = masterReflInfo['measXYO'][hitReflId, :]
        # measAngs = self.detectorGeom.xyoToAng(measXYO[:, 0], measXYO[:, 1], measXYO[:, 2])
        measAngs = tmpDG.xyoToAng(measXYO[:, 0], measXYO[:, 1], measXYO[:, 2])
        measQvec = makeMeasuredScatteringVectors(measAngs[0], measAngs[1], measAngs[2])
        measQvec = num.tile(2*num.sin(0.5*measAngs[0])/wlen, (3, 1)) * measQvec
        
        # predicted Q
        predQvec = num.dot(bMat, measHKLs.T)

        # return value is concatenated vector differences between
        # measured and predicted Q (i.e. G)
        retVal = ( measQvec - predQvec ).T.flatten()
        
        return retVal
    def fit(self, xtol=1e-12, ftol=1e-12, fitPVec=True, display=True, fout=None):
        """
        Fits the cell distortion and orientation with respect to the reference in terms
        of the deformation gradient F = R * U where R is proper orthogonal and U is
        symmetric positive-definite; i.e. the right polar decomposition factors.
        """
        # quit if there aren't enough parameters to have it over determined
        import sys
        fout = fout or sys.stdout

        if len(num.where(self.grainSpots['iRefl'] >= 0)[0]) < 14:
            print >> fout, 'Not enough data for fit, exiting...'
            return
        
        # for initial guess
        angl, axxx = rot.angleAxisOfRotMat(self.rMat)
        R0 = angl * axxx
        E0 = mUtil.symmToVecMV(self.vMat) - [1,1,1,0,0,0] # num.zeros(6)
        if self.detectorGeom.pVec is None:
            p0 = num.zeros(3)
        else:
            p0 = self.detectorGeom.pVec
        
        if fitPVec:
            x0 = num.r_[R0.flatten(), E0, p0]
            lsArgs = ()
        else:
            x0 = num.r_[R0.flatten(), E0]
            lsArgs = (False)
        
        # do least squares
        x1, cov_x, infodict, mesg, ierr = \
            optimize.leastsq(self._fitF_objFunc, 
                             x0, args=lsArgs,
                             xtol=xtol,
                             ftol=ftol,
                             full_output=1)
                
        
        # strip results
        #   - map rotation to fundamental region... necessary?
        q1 = rot.quatOfExpMap(x1[:3].reshape(3, 1))
        # q1 = sym.toFundamentalRegion(q1, crysSym=self.planeData.getLaueGroup() )
        R1 = rot.rotMatOfQuat( q1 )
        E1 = mUtil.vecMVToSymm(x1[3:9])
        if fitPVec:
            p1 = x1[9:]
        
        self.rMat = R1
        self.vMat = mUtil.symmToVecMV(E1 + num.eye(3))
        if fitPVec:
            self.detectorGeom.pVec = p1
        
        # print results
        if display:
            lp = num.array(self.latticeParameters)
            print >> fout, 'final objective function value: %1.3e\n' % (sum(infodict['fvec']**2))
            print >> fout, 'refined orientation: \n' + \
                  '[%1.4e, %1.4e, %1.4e, %1.4e]\n' % (q1[0], q1[1], q1[2], q1[3])
            print >> fout, 'refined biot strain matrix: \n' + \
                  '%1.3e, %1.3e, %1.3e\n' % (E1[0, 0], E1[0, 1], E1[0, 2]) + \
                  '%1.3e, %1.3e, %1.3e\n' % (E1[1, 0], E1[1, 1], E1[1, 2]) + \
                  '%1.3e, %1.3e, %1.3e\n' % (E1[2, 0], E1[2, 1], E1[2, 2])
            print >> fout, 'refined cell parameters: \n' + \
                  '%g\t%g\t%g\t%g\t%g\t%g\n' % tuple(num.r_[lp[:3], r2d*lp[3:]])
            if fitPVec:
                print >> fout, "refined COM coordinates: (%g, %g, %g)\n" % (p1[0], p1[1], p1[2])
        return
    def displaySpots(self):
        grain = self
        masterReflInfo = grain.grainSpots
        iRefl = masterReflInfo['iRefl']
        these = num.where(iRefl >= 0)
        predAngs = masterReflInfo['predAngles'][these]
        xyoPointsListBase = ( 
            [ map(float, self.detectorGeom.angToXYO(*predAng)) for predAng in predAngs ] , 
            {'marker':'o','mec':'r'},
            )
        retval = []
        for iSpot, iHKL, predAng in zip(iRefl[these], masterReflInfo['iHKL'][these], predAngs):
            spot = self.spots.getSpotObj(iSpot)
            xyoThis = map(float, self.detectorGeom.angToXYO(*predAng))
            xyoPointsThis = (
                [ xyoThis ],
                {'marker':'x','mec':'r'},
                )
            retvalThis = spot.display(xyoPointsList=[ xyoPointsListBase, xyoPointsThis ],
                                      title="spot %d"%(iSpot))
            retval.append(retvalThis)
        return retval
    def _minFiberDistance_obj(self, rVec):
        """
        given an orientation computes the distance from that point to all fibers
        associated with the grain.  used to refine orientation.
        """
        masterReflInfo = self.grainSpots
        hitReflId      = num.where(masterReflInfo['iRefl'] >= 0)[0]
        nRefl          = len(hitReflId)

        # wavelength (for normalization)
        wlen = self.planeData.wavelength
        bMat = self.bMat
        
        q = rot.quatOfRotMat(rot.rotMatOfExpMap(num.c_[rVec]))

        # mesured data
        #   - measAngs = [tTh, eta, ome]
        #   - normalized to have length of 1/d
        measHKLs = masterReflInfo['hkl'][hitReflId, :]
        measXYO  = masterReflInfo['measXYO'][hitReflId, :]
        measAngs = self.detectorGeom.xyoToAng(measXYO[:, 0], measXYO[:, 1], measXYO[:, 2])

        # measured Q vectors
        measQvec = makeMeasuredScatteringVectors(measAngs[0], measAngs[1], measAngs[2])
        measQvec = num.tile(2*num.sin(0.5*measAngs[0])/wlen, (3, 1)) * measQvec

        # predicted Q vectors
        predQvec = num.dot( bMat, measHKLs.T )
        
        dist = num.empty(nRefl, dtype=float)
        for i in range(nRefl):
            dist[i] = rot.distanceToFiber(predQvec[:, i].reshape(3, 1),
                                        measQvec[:, i].reshape(3, 1),
                                        q, self.planeData.getQSym())
            pass
        return dist
    def minimizeFiberDistance(self, xtol=1e-12, ftol=1e-12):
        """
        find orientation by minimizing distance to all fibers
        """
        angl, axxx = rot.angleAxisOfRotMat(self.rMat)
        x0 = angl * axxx
        
        optResults = optimize.leastsq(self._minFiberDistance_obj, x0.flatten(), xtol=xtol, ftol=ftol)
        
        x1 = optResults[0]
        r1 = rot.rotMatOfExpMap(x1)
        q1 = rot.quatOfRotMat(r1)
        print 'refined orientation: ' + \
              '[%1.4e, %1.4e, %1.4e, %1.4e]\n' % (q1[0], q1[1], q1[2], q1[3])
        self.rMat = r1
        return
    def strip(self):
        """
        meant for multiprocessing, to strip out things that do not really need to be pickled and sent
        """
        self.spots     = None
        self.planeData = None
        return
    def restore(self, other):
        self.spots     = other.spots
        self.planeData = other.planeData
        return

def validateQVecAngles(angList, angMin, angMax):
    angList = num.atleast_1d(angList)   # needs to have 'shape'
    
    angMin = num.atleast_1d(angMin)    # need to have len
    angMax = num.atleast_1d(angMax)    # need to have len
    
    assert len(angMin) == len(angMax), "length of min and max angular limits must match!"

    reflInRange = num.zeros(angList.shape, dtype=bool)

    """
    the algorithm won't work if the range is >= pi; therefore we have to pre-process
    split up the ranges, and split them into chunks of, say, 90 degrees as necessary
    """
    angRange_l = []
    for i in range(len(angMin)):
        if abs(angMax[i] - angMin[i]) >= num.pi:
            tmpRange = num.r_[num.arange(angMin[i], angMax[i], 0.5*num.pi), angMax[i]]
            tmpRange = num.c_[tmpRange[:-1], tmpRange[1:]].tolist()
            for j in range(len(tmpRange)):
                angRange_l.append(tmpRange[j])
        else:
            angRange_l.append([angMin[i], angMax[i]])
            pass
        pass
    for i in range(len(angRange_l)):
        #
        limVecMin = num.r_[num.cos(angRange_l[i][0]), num.sin(angRange_l[i][0])]
        limVecMax = num.r_[num.cos(angRange_l[i][1]), num.sin(angRange_l[i][1])]
        #
        wedgeAngle = num.arccos( num.dot( limVecMin, limVecMax ) )
        #
        tVec = num.c_[ num.cos(angList), num.sin(angList) ]
        #
        dp = num.arccos( num.dot(tVec, limVecMin) ) + num.arccos( num.dot(tVec, limVecMax) )
        #
        reflInRange = reflInRange | ( abs(dp - wedgeAngle) <= num.sqrt(rot.tinyRotAng) )
    return reflInRange